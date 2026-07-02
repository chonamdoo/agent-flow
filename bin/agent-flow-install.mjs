#!/usr/bin/env node
// Project-local installer for agent-flow.
//
// Run from any project root:
//   npx <agent-flow-package> install
//
// The installer creates .agent-flow/ (runs, memory, kit metadata) and
// upserts an agent-flow block into CLAUDE.md / AGENTS.md so
// every host CLI sees the same workflow contract.

import fs from "node:fs";
import crypto from "node:crypto";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { SKILL_DEPENDENCIES, mergeInstallSelectionWithPrevious, resolveInstallSelection } from "../lib/skill-selection.mjs";

const KIT_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const AGENT_FLOW_COMMAND = "agent-flow";
const INSTALL_ARGS = process.argv.slice(3);
const FORCE_MANAGED = INSTALL_ARGS.includes("--force-managed");
const REQUESTED_PROJECT = process.cwd();
const HOME = process.env.HOME || process.env.USERPROFILE || "";
const PROJECT = resolveInstallProject(REQUESTED_PROJECT);
const AF_DIR = path.join(PROJECT, ".agent-flow");
const PROJECT_SKILL_HOSTS = Object.freeze(["claude", "codex", "omp"]);
const BUNDLED_HOST_SKILL_NAMES = new Set([
  "agent-flow",
  "android-appshell-error-handling",
  "comment-authoring-discipline",
  "comment-checker",
  "ios-app-shell-error-handling",
  "react-app-shell-error-handling",
  "react-native-app-shell-error-handling",
]);
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
const GENERATED_PROJECT_SKILL_NAMES = new Set([
  "architecture-reviewer",
  "full-feature-workflow",
  "plan-reviewer",
  "product-brief",
  "push-watch",
]);

function ensureDir(p) {
  fs.mkdirSync(p, { recursive: true });
}

function resolveManagedWorktreeRoot(start) {
  const parts = path.resolve(start).split(path.sep);
  const markers = new Set([".agent-flow", ".codex", ".Codex", ".omp"]);
  for (let index = parts.length - 2; index >= 0; index -= 1) {
    if (parts[index + 1] !== "worktrees") continue;
    if (!markers.has(parts[index])) continue;
    const root = parts.slice(0, index).join(path.sep) || path.sep;
    if (HOME && samePath(root, HOME) && (parts[index] === ".codex" || parts[index] === ".Codex" || parts[index] === ".omp")) {
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
  if (
      fs.existsSync(path.join(PROJECT, "Package.swift")) ||
      hasChildWithSuffix(PROJECT, ".xcodeproj") ||
      hasChildWithSuffix(PROJECT, ".xcworkspace")
  ) {
    return "ios";
  }
  if (fs.existsSync(path.join(PROJECT, "pyproject.toml")) ||
      fs.existsSync(path.join(PROJECT, "requirements.txt"))) {
    return "python";
  }
  const earlyPackagePath = path.join(PROJECT, "package.json");
  if (fs.existsSync(earlyPackagePath)) {
    const packageText = fs.readFileSync(earlyPackagePath, "utf8");
    if (packageText.includes("react-native")) {
      return "react-native";
    }
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

function hasChildWithSuffix(rootDir, suffix) {
  if (!fs.existsSync(rootDir)) {
    return false;
  }
  return fs.readdirSync(rootDir).some((name) => name.endsWith(suffix));
}

function copyDir(
  src,
  dest,
  excludedRootDirs = new Set(),
  isRoot = true,
  force = false,
  pruneExtraneous = false,
  preservedExtraneousRootNames = new Set(),
  allowedRootDirs = null,
) {
  // Recursive copy without overwriting user-modified files. If a file exists
  // at dest with different content, leave it (user customization wins) and
  // print a notice. Brand-new files are always written.
  if (!fs.existsSync(src)) return { written: 0, skipped: 0 };
  let written = 0, skipped = 0;
  ensureDir(dest);
  const sourceNames = new Set();
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    sourceNames.add(entry.name);
    const srcPath = path.join(src, entry.name);
    const destPath = path.join(dest, entry.name);
    if (entry.isDirectory()) {
      if (isRoot && allowedRootDirs && !allowedRootDirs.has(entry.name)) {
        const r = removeDirIfSame(srcPath, destPath, force);
        written += r.written;
        skipped += r.skipped;
        continue;
      }
      if (isRoot && excludedRootDirs.has(entry.name)) {
        const r = removeDirIfSame(srcPath, destPath, force);
        written += r.written;
        skipped += r.skipped;
        continue;
      }
      const r = copyDir(
        srcPath,
        destPath,
        excludedRootDirs,
        false,
        force,
        pruneExtraneous,
        preservedExtraneousRootNames,
        null,
      );
      written += r.written;
      skipped += r.skipped;
    } else if (entry.isFile()) {
      if (fs.existsSync(destPath)) {
        const srcContent = fs.readFileSync(srcPath, "utf8");
        const destContent = fs.readFileSync(destPath, "utf8");
        if (srcContent !== destContent && !force) {
          skipped += 1;
          console.log(`  ! skipped (user-modified): ${path.relative(PROJECT, destPath)}`);
          continue;
        }
      }
      fs.copyFileSync(srcPath, destPath);
      written += 1;
    }
  }
  if (force && pruneExtraneous) {
    for (const entry of fs.readdirSync(dest, { withFileTypes: true })) {
      if (!sourceNames.has(entry.name) && !(isRoot && preservedExtraneousRootNames.has(entry.name))) {
        fs.rmSync(path.join(dest, entry.name), { recursive: true, force: true });
      }
    }
  }
  return { written, skipped };
}

function removeDirIfSame(src, dest, force = false) {
  if (!fs.existsSync(dest)) return { written: 0, skipped: 0 };
  if (force) {
    fs.rmSync(dest, { recursive: true, force: true });
    return { written: 1, skipped: 0 };
  }
  if (!dirContentsMatch(src, dest)) return { written: 0, skipped: 1 };
  fs.rmSync(dest, { recursive: true, force: true });
  return { written: 1, skipped: 0 };
}

function removeStaleContextDocsScripts(agentFlowDir, force = false) {
  if (!force) return;
  for (const filename of ["check-context-docs.mjs", "check-context-docs.ts"]) {
    fs.rmSync(path.join(agentFlowDir, "scripts", filename), { force: true });
  }
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

function copyFileIfMissingOrSame(src, dest, force = false) {
  if (!fs.existsSync(src)) return false;
  ensureDir(path.dirname(dest));
  const srcContent = fs.readFileSync(src, "utf8");
  if (force) {
    fs.copyFileSync(src, dest);
    return true;
  }
  if (fs.existsSync(dest) && fs.readFileSync(dest, "utf8") !== srcContent) {
    console.log(`  ! skipped (user-modified): ${path.relative(PROJECT, dest)}`);
    return false;
  }
  fs.copyFileSync(src, dest);
  return true;
}

function writeFileIfMissingOrSame(dest, content, force = false) {
  ensureDir(path.dirname(dest));
  if (force) {
    fs.writeFileSync(dest, content, "utf8");
    return true;
  }
  if (fs.existsSync(dest) && fs.readFileSync(dest, "utf8") !== content) {
    console.log(`  ! skipped (user-modified): ${path.relative(PROJECT, dest)}`);
    return false;
  }
  fs.writeFileSync(dest, content, "utf8");
  return true;
}

function writeManagedFile(pathName, content) {
  ensureDir(path.dirname(pathName));
  fs.writeFileSync(pathName, content, "utf8");
}

function shellQuote(value) {
  return `'${String(value).replaceAll("'", "'\\''")}'`;
}

function hookScriptCommand(root, scriptName) {
  return shellQuote(path.join(root, ".agent-flow", "scripts", "hooks", scriptName));
}

function codexHooksSettings(root) {
  return {
    hooks: {
      PreToolUse: [
        {
          matcher: "Bash",
          hooks: [
            { type: "command", command: hookScriptCommand(root, "guard-worktree.sh") },
            { type: "command", command: hookScriptCommand(root, "guard-protected-branch.sh") },
          ],
        },
      ],
      PostToolUse: [
        {
          matcher: "^(apply_patch|Write|Edit|MultiEdit|write|edit|multi_edit|multiedit)$",
          hooks: [{ type: "command", command: hookScriptCommand(root, "comment-checker.py") }],
        },
      ],
      Stop: [
        {
          hooks: [{ type: "command", command: hookScriptCommand(root, "show-phase-status.sh") }],
        },
      ],
    },
  };
}

function unquoteShellWord(value) {
  if (typeof value !== "string") {
    return "";
  }
  if (value.startsWith("'") && value.endsWith("'")) {
    return value.slice(1, -1).replaceAll("'\\''", "'");
  }
  return value;
}

function managedHookScriptName(command) {
  const normalized = unquoteShellWord(command).replaceAll("\\", "/").replaceAll("'", "").replaceAll('"', "");
  for (const scriptName of ["guard-worktree.sh", "guard-protected-branch.sh", "show-phase-status.sh", "comment-checker.py"]) {
    if (
      normalized === `.agent-flow/scripts/hooks/${scriptName}` ||
      normalized === `scripts/hooks/${scriptName}` ||
      normalized.endsWith(`/.agent-flow/scripts/hooks/${scriptName}`) ||
      normalized.endsWith(`/scripts/hooks/${scriptName}`) ||
      normalized.includes(`/.agent-flow/scripts/hooks/${scriptName}`) ||
      normalized.includes(`/scripts/hooks/${scriptName}`)
    ) {
      return scriptName;
    }
  }
  return null;
}

function trustedManagedHookScriptName(root, command) {
  const normalized = unquoteShellWord(command).replaceAll("\\", "/");
  const normalizedRoot = path.resolve(root).replaceAll("\\", "/");
  for (const scriptName of ["guard-worktree.sh", "guard-protected-branch.sh", "show-phase-status.sh", "comment-checker.py"]) {
    if (normalized === `${normalizedRoot}/.agent-flow/scripts/hooks/${scriptName}`) {
      return scriptName;
    }
  }
  return null;
}

function mergeHookSettings(settings, desired) {
  if (!settings.hooks) {
    settings.hooks = {};
  }
  for (const [event, entries] of Object.entries(desired)) {
    if (!settings.hooks[event]) {
      settings.hooks[event] = [];
    }
    for (const entry of entries) {
      const existing = settings.hooks[event].find((e) => (e.matcher ?? "") === (entry.matcher ?? ""));
      if (existing) {
        if (!existing.hooks) {
          existing.hooks = [];
        }
        for (const hook of entry.hooks) {
          const scriptName = managedHookScriptName(hook.command);
          const matchingHook = existing.hooks.find(
            (h) => scriptName && managedHookScriptName(h.command) === scriptName,
          );
          if (matchingHook) {
            Object.assign(matchingHook, hook);
          } else if (!existing.hooks.some((h) => h.command === hook.command)) {
            existing.hooks.push(hook);
          }
        }
      } else {
        settings.hooks[event].push(entry);
      }
    }
  }
}

function readHookSettings(settingsPath) {
  if (!fs.existsSync(settingsPath)) {
    return {};
  }
  try {
    return JSON.parse(fs.readFileSync(settingsPath, "utf8"));
  } catch {
    const backupPath = `${settingsPath}.bak`;
    fs.copyFileSync(settingsPath, backupPath);
    console.error(`warning: could not parse ${settingsPath}; backed up to ${backupPath} before overwriting`);
    return {};
  }
}

function mergeHookConfig(settings, source) {
  if (!source || typeof source !== "object") {
    return;
  }
  for (const [key, value] of Object.entries(source)) {
    if (key !== "hooks" && settings[key] === undefined) {
      settings[key] = value;
    }
  }
  if (source.hooks) {
    mergeHookSettings(settings, source.hooks);
  }
}

function escapeRegex(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function tomlBasicString(value) {
  return String(value).replaceAll("\\", "\\\\").replaceAll("\"", "\\\"");
}

function upsertTomlValue(text, tableHeader, key, value) {
  const tableName = tableHeader.slice(1, -1);
  const tablePattern = new RegExp(`(^|\\n)\\s*\\[\\s*${escapeRegex(tableName)}\\s*\\]\\s*(?:#.*)?\\n([\\s\\S]*?)(?=\\n\\s*\\[[^\\n]+\\]|$)`);
  const keyPattern = new RegExp(`(^|\\n)\\s*${escapeRegex(key)}\\s*=.*(?=\\n|$)`);
  const match = text.match(tablePattern);
  if (!match) {
    const prefix = text.trim() ? `${text.replace(/\n*$/, "\n\n")}` : "";
    return `${prefix}${tableHeader}\n${key} = ${value}\n`;
  }
  return text.replace(tablePattern, (full, leading, body) => {
    const nextBody = keyPattern.test(body)
      ? body.replace(keyPattern, `$1${key} = ${value}`)
      : `${body.replace(/\n*$/, "")}\n${key} = ${value}\n`;
    return `${leading}${tableHeader}\n${nextBody}`;
  });
}

function codexConfigPath() {
  if (!HOME) {
    return null;
  }
  return path.join(HOME, ".codex", "config.toml");
}

function upsertCodexConfigTableValue(tableHeader, key, value) {
  const configPath = codexConfigPath();
  if (!configPath) {
    return false;
  }
  const current = fs.existsSync(configPath) ? fs.readFileSync(configPath, "utf8") : "";
  const next = upsertTomlValue(current, tableHeader, key, value);
  if (next === current) {
    return true;
  }
  fs.mkdirSync(path.dirname(configPath), { recursive: true });
  fs.writeFileSync(configPath, next.endsWith("\n") ? next : `${next}\n`, "utf8");
  return true;
}

function resolveCodexBinary() {
  const candidates = [
    process.env.CODEX_CLI_PATH,
    "/Applications/Codex.app/Contents/Resources/codex",
    "codex",
  ].filter(Boolean);
  for (const candidate of candidates) {
    const result = spawnSync(candidate, ["--version"], { encoding: "utf8", timeout: 3000 });
    if (!result.error && result.status === 0) {
      return candidate;
    }
  }
  return null;
}

function queryCodexProjectHookHashes(root) {
  const codexBinary = resolveCodexBinary();
  if (!codexBinary) {
    return [];
  }
  const helper = String.raw`
const { spawn } = require("child_process");
const codexBinary = process.argv[1];
const root = process.argv[2];
const responses = [];
let finished = false;
let stdoutBuffer = "";
const proc = spawn(codexBinary, ["app-server", "--stdio"], { stdio: ["pipe", "pipe", "pipe"] });
proc.stdout.on("data", (chunk) => {
  stdoutBuffer += chunk.toString();
  let newlineIndex;
  while ((newlineIndex = stdoutBuffer.indexOf("\n")) !== -1) {
    handleLine(stdoutBuffer.slice(0, newlineIndex));
    stdoutBuffer = stdoutBuffer.slice(newlineIndex + 1);
  }
});
proc.stderr.on("data", () => {});
function handleLine(line) {
  const trimmed = line.trim();
  if (!trimmed) {
    return;
  }
  try {
    const response = JSON.parse(trimmed);
    responses.push(response);
    if (response.id === 2) {
      finish(response);
    }
  } catch {
    // Ignore non-JSON app-server output.
  }
}
function send(id, method, params) {
  proc.stdin.write(JSON.stringify({ id, method, params }) + "\n");
}
setTimeout(() => {
  send(1, "initialize", {
    clientInfo: { name: "agent-flow-install", title: null, version: "1" },
    capabilities: { experimentalApi: true, requestAttestation: false },
  });
  setTimeout(() => send(2, "hooks/list", { cwds: [root] }), 250);
}, 50);
function finish(response) {
  if (finished) {
    return;
  }
  finished = true;
  if (!response || response.error) {
    proc.kill("SIGTERM");
    process.exit(1);
  }
  const entry = response.result?.data?.find((item) => item.cwd === root);
  const sourcePaths = new Set([root + "/.Codex/hooks.json", root + "/.codex/hooks.json"]);
  const hooks = (entry?.hooks ?? [])
    .filter((hook) => sourcePaths.has(hook.sourcePath) && hook.key && hook.currentHash)
    .map((hook) => ({ key: hook.key, trustedHash: hook.currentHash, command: hook.command ?? "" }));
  console.log(JSON.stringify(hooks));
  proc.kill("SIGTERM");
}
const timer = setTimeout(() => finish(responses.find((item) => item.id === 2)), 3000);
proc.on("exit", () => {
  if (stdoutBuffer.trim()) {
    handleLine(stdoutBuffer);
    stdoutBuffer = "";
  }
  clearTimeout(timer);
  if (!finished) {
    finish(responses.find((item) => item.id === 2));
  }
});
`;
  const result = spawnSync(process.execPath, ["-e", helper, codexBinary, root], {
    encoding: "utf8",
    timeout: 8000,
  });
  if (result.error || result.status !== 0) {
    return [];
  }
  try {
    const parsed = JSON.parse(result.stdout.trim());
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function installCodexTrustState(root) {
  if (process.env.AGENT_FLOW_SKIP_CODEX_TRUST === "1") {
    return;
  }
  const projectHeader = `[projects."${tomlBasicString(root)}"]`;
  if (!upsertCodexConfigTableValue(projectHeader, "trust_level", "\"trusted\"")) {
    console.error("warning: Codex project trust not registered; HOME is unavailable");
    return;
  }
  const hooks = queryCodexProjectHookHashes(root);
  const managedHooks = hooks.filter((hook) => trustedManagedHookScriptName(root, hook.command));
  if (managedHooks.length === 0) {
    console.error("warning: Codex hook trust not registered; codex app-server did not return project hooks");
    return;
  }
  for (const hook of managedHooks) {
    const hookHeader = `[hooks.state."${tomlBasicString(hook.key)}"]`;
    upsertCodexConfigTableValue(hookHeader, "trusted_hash", `"${tomlBasicString(hook.trustedHash)}"`);
  }
}

function installCodexHooks(root) {
  const settingsPaths = [
    path.join(root, ".Codex", "hooks.json"),
    path.join(root, ".codex", "hooks.json"),
  ];
  const settings = {};
  for (const settingsPath of settingsPaths) {
    mergeHookConfig(settings, readHookSettings(settingsPath));
  }
  mergeHookSettings(settings, codexHooksSettings(root).hooks);
  for (const settingsPath of settingsPaths) {
    ensureDir(path.dirname(settingsPath));
    fs.writeFileSync(settingsPath, `${JSON.stringify(settings, null, 2)}\n`, "utf8");
  }
  installCodexTrustState(root);
  return true;
}

function claudeHooksSettings(root) {
  return {
    hooks: {
      PreToolUse: [
        {
          matcher: "Bash",
          hooks: [
            { type: "command", command: hookScriptCommand(root, "guard-worktree.sh") },
            { type: "command", command: hookScriptCommand(root, "guard-protected-branch.sh") },
          ],
        },
      ],
      PostToolUse: [
        {
          matcher: "^(apply_patch|Write|Edit|MultiEdit|write|edit|multi_edit|multiedit)$",
          hooks: [{ type: "command", command: hookScriptCommand(root, "comment-checker.py") }],
        },
      ],
      Stop: [
        {
          hooks: [{ type: "command", command: hookScriptCommand(root, "show-phase-status.sh") }],
        },
      ],
    },
  };
}

function installClaudeHooks(root) {
  const settingsPath = path.join(root, ".claude", "settings.json");
  const settings = readHookSettings(settingsPath);
  mergeHookSettings(settings, claudeHooksSettings(root).hooks);
  ensureDir(path.dirname(settingsPath));
  fs.writeFileSync(settingsPath, `${JSON.stringify(settings, null, 2)}\n`, "utf8");
}

function ompHooksExtensionSource() {
  return String.raw`import fs from "node:fs";
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const HOOK_DIR = path.join(ROOT, ".agent-flow", "scripts", "hooks");
const WRITE_TOOL_RE = /^(apply_patch|Write|Edit|MultiEdit|write|edit|multi_edit|multiedit)$/i;

export default function agentFlowHooks(pi) {
  if (typeof pi.setLabel === "function") {
    pi.setLabel("agent-flow hooks");
  }


  pi.on("context", async (event) => {
    const messages = Array.isArray(event?.messages) ? event.messages : [];
    const filtered = messages.filter((message) => {
      if (message?.customType === "agent-flow-model-context" || message?.details?.source === "agent-flow-omp-model-context") {
        return false;
      }
      if (message?.role === "user") {
        return true;
      }
      const text = messageText(message).trim();
      return !(text.startsWith("<context>") && text.endsWith("</context>") && /<file\b[^>]*\bsource="agent-flow-omp-model-context"/.test(text));
    });
    if (filtered.length !== messages.length) {
      return { messages: filtered };
    }
  });
  pi.on("tool_call", async (event, ctx) => {
    if (!isBashTool(event?.toolName)) {
      return;
    }
    const payload = hookPayload(event, ctx);
    for (const scriptName of ["guard-worktree.sh", "guard-protected-branch.sh"]) {
      const result = await runHook(scriptName, payload, ctx);
      if (result.block) {
        return { block: true, reason: result.reason };
      }
    }
  });

  pi.on("tool_result", async (event, ctx) => {
    if (!WRITE_TOOL_RE.test(String(event?.toolName || ""))) {
      return;
    }
    const syncError = syncRootContextFiles(event, ctx);
    if (syncError) {
      return {
        content: [{ type: "text", text: syncError }],
        details: { agentFlowHook: "sync-root-context" },
        isError: true,
      };
    }
    const result = await runHook("comment-checker.py", hookPayload(event, ctx), ctx);
    if (result.block) {
      return {
        content: [{ type: "text", text: result.reason }],
        details: { agentFlowHook: "comment-checker.py" },
        isError: true,
      };
    }
  });

  pi.on("session_shutdown", async (_event, ctx) => {
    const result = await runHook("show-phase-status.sh", { hook_event_name: "session_shutdown" }, ctx);
    const message = parseSystemMessage(result.reason);
    if (message && ctx?.hasUI && typeof ctx.ui?.notify === "function") {
      await ctx.ui.notify(message, "info");
    }
  });
}


function hookPayload(event, ctx) {
  const input = event?.input || {};
  const toolName = String(event?.toolName || "");
  return {
    tool_name: toolName,
    tool: toolName,
    hook_event_name: String(event?.type || ""),
    tool_input: input,
    input,
    parameters: input,
    cwd: ctx?.cwd || ROOT,
  };
}

function messageText(message) {
  const content = message?.content;
  if (typeof content === "string") {
    return content;
  }
  if (!Array.isArray(content)) {
    return "";
  }
  return content.map((part) => typeof part?.text === "string" ? part.text : "").join("\n");
}


function pathExists(filePath) {
  try {
    fs.statSync(filePath);
    return true;
  } catch {
    return false;
  }
}


function syncRootContextFiles(event, ctx) {
  const direction = rootContextSyncDirection(event, ctx);
  if (!direction) {
    return "";
  }
  try {
    const content = fs.readFileSync(direction.sourcePath, "utf8");
    const current = pathExists(direction.destPath) ? fs.readFileSync(direction.destPath, "utf8") : "";
    if (current !== content) {
      fs.writeFileSync(direction.destPath, content, "utf8");
    }
    return "";
  } catch (error) {
    return "agent-flow hook failed to sync " + direction.sourceName + " to " + direction.destName + ": " + String(error?.message || error);
  }
}

function rootContextSyncDirection(event, ctx) {
  const changed = modifiedRootContextFiles(event?.input, ctx?.cwd || ROOT);
  if (changed.has("CLAUDE.md")) {
    return {
      sourceName: "CLAUDE.md",
      destName: "AGENTS.md",
      sourcePath: path.join(ROOT, "CLAUDE.md"),
      destPath: path.join(ROOT, "AGENTS.md"),
    };
  }
  if (changed.has("AGENTS.md")) {
    return {
      sourceName: "AGENTS.md",
      destName: "CLAUDE.md",
      sourcePath: path.join(ROOT, "AGENTS.md"),
      destPath: path.join(ROOT, "CLAUDE.md"),
    };
  }
  return null;
}

function modifiedRootContextFiles(input, cwd) {
  const changed = new Set();
  for (const filePath of collectModifiedPaths(input)) {
    const fileName = rootContextFileName(filePath, cwd);
    if (fileName) {
      changed.add(fileName);
    }
  }
  return changed;
}

function collectModifiedPaths(input) {
  const paths = [];
  const visit = (value) => {
    if (typeof value === "string") {
      paths.push(...pathsFromPatch(value));
      return;
    }
    if (Array.isArray(value)) {
      for (const item of value) {
        visit(item);
      }
      return;
    }
    if (!value || typeof value !== "object") {
      return;
    }
    for (const key of ["file_path", "filePath", "path", "filename"]) {
      if (typeof value[key] === "string") {
        paths.push(value[key]);
      }
    }
    for (const key of ["patch", "command"]) {
      if (typeof value[key] === "string") {
        paths.push(...pathsFromPatch(value[key]));
      }
    }
    if (Array.isArray(value.edits)) {
      visit(value.edits);
    }
  };
  visit(input);
  return paths;
}

function pathsFromPatch(text) {
  if (!text.includes("CLAUDE.md") && !text.includes("AGENTS.md")) {
    return [];
  }
  const paths = [];
  for (const line of text.split(/\r?\n/)) {
    const tagged = line.match(/^\[([^#\]\r\n]+)#[0-9A-Fa-f]+\]$/);
    if (tagged) {
      paths.push(tagged[1]);
      continue;
    }
    const unified = line.match(/^\*\*\* (?:Add|Update|Delete) File: (.+)$/);
    if (unified) {
      paths.push(unified[1].trim());
    }
  }
  return paths;
}

function rootContextFileName(filePath, cwd) {
  const resolved = path.resolve(cwd || ROOT, filePath);
  for (const fileName of ["CLAUDE.md", "AGENTS.md"]) {
    if (samePath(resolved, path.join(ROOT, fileName))) {
      return fileName;
    }
  }
  return "";
}

function samePath(left, right) {
  return path.resolve(left) === path.resolve(right);
}

function isBashTool(toolName) {
  return /^(Bash|bash)$/.test(String(toolName || ""));
}

async function runHook(scriptName, payload, ctx) {
  const scriptPath = path.join(HOOK_DIR, scriptName);
  const result = await spawnHook(scriptPath, JSON.stringify(payload), ctx?.cwd || ROOT);
  const reason = (result.stderr || result.stdout || "").trim();
  if (result.status === 0) {
    return { block: false, reason };
  }
  return { block: true, reason: reason || "agent-flow hook blocked: " + scriptName };
}

function spawnHook(scriptPath, input, cwd) {
  return new Promise((resolve) => {
    const proc = spawn(scriptPath, [], { cwd, stdio: ["pipe", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    let settled = false;
    const finish = (result) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      resolve(result);
    };
    const timer = setTimeout(() => {
      try {
        proc.kill("SIGTERM");
      } catch {
      }
      finish({ status: 124, stdout, stderr: stderr || "agent-flow hook timed out" });
    }, 8000);
    proc.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    proc.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    proc.on("error", () => {
      finish({ status: 0, stdout: "", stderr: "" });
    });
    proc.on("close", (status) => {
      finish({ status: status ?? 0, stdout, stderr });
    });
    proc.stdin.end(input);
  });
}

function parseSystemMessage(text) {
  if (!text) {
    return "";
  }
  try {
    const parsed = JSON.parse(text);
    return String(parsed.systemMessage || "");
  } catch {
    return text;
  }
}
`;
}

function installOmpHooks(root) {
  return writeFileIfMissingOrSame(
    path.join(root, ".omp", "extensions", "agent-flow-hooks.ts"),
    ompHooksExtensionSource(),
    FORCE_MANAGED,
  );
}

function makeHooksExecutable(root) {
  const hooksDir = path.join(root, ".agent-flow", "scripts", "hooks");
  if (!fs.existsSync(hooksDir)) {
    return;
  }
  for (const entry of fs.readdirSync(hooksDir)) {
    if (entry.endsWith(".sh") || entry === "comment-checker.py") {
      fs.chmodSync(path.join(hooksDir, entry), 0o755);
    }
  }
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

function installProjectSkills(forceManaged = false, installSelection = null) {
  const previousIndex = readJsonIfExists(path.join(AF_DIR, "skills", "index.json"));
  const selected = selectProjectSkills(installSelection);
  const links = [];
  for (const skill of selected.skills) {
    // bundled skill 중 host 디렉토리 link 대상은 BUNDLED_HOST_SKILL_NAMES뿐이다.
    // 나머지 bundled skill은 index에만 노출해 agent가 발견할 수 있게 한다.
    if (skill.source === "bundled" && !BUNDLED_HOST_SKILL_NAMES.has(skill.name)) {
      continue;
    }
    for (const host of skill.hosts) {
      links.push(linkProjectSkill(skill, host, previousIndex, forceManaged));
    }
  }
  links.push(...removeStaleProjectSkillLinks(selected.skills, previousIndex, forceManaged));
  const index = { ...selected, links };
  fs.writeFileSync(path.join(AF_DIR, "skills", "index.json"), `${JSON.stringify(index, null, 2)}\n`);
  return index;
}

function selectProjectSkills(installSelection = null) {
  const discovered = [
    ...discoverSkills(path.join(AF_DIR, "local-skills"), "local", PROFILE_MANAGED_HOST_ONLY_SKILLS),
    ...discoverProjectSkills(),
    ...discoverSkills(path.join(AF_DIR, "skills"), "bundled", PROFILE_MANAGED_HOST_ONLY_SKILLS),
  ];
  const byName = new Map();
  const warnings = [];
  for (const skill of discovered) {
    const current = byName.get(skill.name);
    if (!current || skill.priority < current.priority) byName.set(skill.name, skill);
    warnings.push(...skill.warnings);
  }
  const allowed = installSelection?.skillNames || null;
  const skills = [...byName.values()]
    .filter((skill) => !allowed || allowed.has(skill.name))
    .sort((a, b) => a.name.localeCompare(b.name));
  warnings.push(...validateSkillDependencies(skills));
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
    selection: {
      mode: allowed ? "filtered" : "all",
      profiles: installSelection?.profiles || [],
      explicit_skills: installSelection?.explicitSkills || [],
    },
    skills: skills.map(({ priority, warnings: _warnings, ...skill }) => skill),
    conflicts,
    warnings,
  };
}

function validateSkillDependencies(skills) {
  const names = new Set(skills.map((skill) => skill.name));
  const warnings = [];
  for (const skill of skills) {
    for (const required of skill.requires || []) {
      if (!names.has(required)) {
        warnings.push(`${skill.name}: missing required skill ${required}`);
      }
    }
  }
  return warnings;
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
      id: metadata.id,
      name: metadata.name,
      title: metadata.title,
      path: relativePath,
      source,
      hosts: metadata.hosts,
      requires: metadata.requires,
      dependencies: metadata.dependencies,
      optionalDependencies: metadata.optionalDependencies,
      platforms: metadata.platforms,
      stacks: metadata.stacks,
      references: metadata.references,
      hostSupport: metadata.hostSupport,
      workflowPhases: metadata.workflowPhases,
      reviewAngles: metadata.reviewAngles,
      installGroup: metadata.installGroup,
      excludes: metadata.excludes,
      tags: metadata.tags,
      description: metadata.description,
      trigger: metadata.trigger,
      triggers: metadata.triggers,
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
  const knownHosts = new Set(PROJECT_SKILL_HOSTS);
  const hosts = [];
  for (const host of hostValues) {
    const normalized = String(host).trim().toLowerCase();
    if (knownHosts.has(normalized)) hosts.push(normalized);
    else if (normalized) warnings.push(`unknown host ignored: ${normalized}`);
  }
  const body = text.replace(/^---\n[\s\S]*?\n---\n?/, "");
  const useWhen = body.split(/\r?\n/).find((line) => /^\s*use when\b/i.test(line));
  return {
    id: String(metadata.id || name),
    name,
    title: String(metadata.title || ""),
    description: String(metadata.description || useWhen || ""),
    hosts: hostValues.length > 0 ? [...new Set(hosts)] : [...PROJECT_SKILL_HOSTS],
    tags: Array.isArray(metadata.tags) ? metadata.tags.map(String) : [],
    trigger: String(metadata.trigger || metadata.description || useWhen || ""),
    triggers: arrayValue(metadata.triggers),
    platforms: arrayValue(metadata.platforms),
    stacks: arrayValue(metadata.stacks),
    dependencies: uniqueStrings([...arrayValue(metadata.dependencies), ...arrayValue(metadata.requires)]),
    requires: uniqueStrings([...skillRequires(name), ...arrayValue(metadata.dependencies), ...arrayValue(metadata.requires)]),
    optionalDependencies: arrayValue(metadata.optionalDependencies),
    references: arrayValue(metadata.references),
    hostSupport: arrayValue(metadata.hostSupport),
    workflowPhases: arrayValue(metadata.workflowPhases),
    reviewAngles: arrayValue(metadata.reviewAngles),
    installGroup: String(metadata.installGroup || ""),
    excludes: arrayValue(metadata.excludes || metadata.conflicts),
    warnings,
  };
}

function skillRequires(name) {
  return SKILL_DEPENDENCIES.get(name) || [];
}

function arrayValue(value) {
  return Array.isArray(value) ? value.map(String) : [];
}

function uniqueStrings(values) {
  return [...new Set(values.map(String).filter(Boolean))];
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

function removeStaleProjectSkillLinks(skills, previousIndex, forceManaged = false) {
  if (!previousIndex || !Array.isArray(previousIndex.links)) return [];
  const desired = new Set(skills.flatMap((skill) => skill.hosts.map((host) => `${host}:${skill.name}`)));
  const removed = [];
  for (const link of previousIndex.links) {
    if (!link || !link.name || !link.host || !link.path) continue;
    if (desired.has(`${link.host}:${link.name}`)) continue;
    const target = path.join(PROJECT, link.path);
    // 과거 index는 .codex(소문자) 경로를 기록했다. case-sensitive FS에서
    // ensureChildPath가 .Codex와 어긋나 throw하지 않도록 기록된 casing을 따른다.
    const hostRoot = legacyHostSkillRoot(link.path) ?? hostSkillRoot(link.host);
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
    if (forceManaged) {
      fs.rmSync(target, { recursive: true, force: true });
      removed.push({ name: link.name, host: link.host, path: link.path, status: "removed-stale-forced" });
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

function linkProjectSkill(skill, host, previousIndex, forceManaged = false) {
  const srcDir = path.dirname(path.join(PROJECT, skill.path));
  const hostRoot = hostSkillRoot(host);
  if (pathHasSymlink(PROJECT, hostRoot)) {
    return { name: skill.name, host, path: path.relative(PROJECT, hostRoot), status: "skipped-host-root-symlink" };
  }
  const destDir = path.join(hostRoot, skill.name);
  ensureChildPath(hostRoot, destDir);
  const destSkill = path.join(destDir, "SKILL.md");
  const previousHash = previousSkillHash(previousIndex, skill.name);
  if (fs.existsSync(destDir)) {
    const stat = fs.lstatSync(destDir);
    if (forceManaged) {
      if (stat.isSymbolicLink()) fs.unlinkSync(destDir);
      else fs.rmSync(destDir, { recursive: true, force: true });
    } else if (stat.isSymbolicLink()) fs.unlinkSync(destDir);
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

function hostSkillRoot(host) {
  // case-sensitive FS에서 .codex/.Codex가 갈라지지 않도록 .Codex로 고정한다.
  if (host === "codex") {
    return path.join(PROJECT, ".Codex", "skills");
  }
  if (host === "omp") {
    return path.join(PROJECT, ".omp", "skills");
  }
  return path.join(PROJECT, `.${host}`, "skills");
}

function legacyHostSkillRoot(linkPath) {
  const normalized = String(linkPath).replaceAll("\\", "/");
  if (normalized.startsWith(".codex/skills/")) {
    return path.join(PROJECT, ".codex", "skills");
  }
  // gemini/antigravity host는 제거됐지만 과거 index가 기록한 link 정리는
  // 계속돼야 한다. hostSkillRoot로 유도하면 .antigravity/skills처럼 실제
  // 경로와 어긋나 ensureChildPath가 throw하며 install이 중단된다.
  if (normalized.startsWith(".gemini/antigravity/skills/")) {
    return path.join(PROJECT, ".gemini", "antigravity", "skills");
  }
  if (normalized.startsWith(".gemini/skills/")) {
    return path.join(PROJECT, ".gemini", "skills");
  }
  return null;
}

function installManagedWorkflowSkills() {
  const generatedSkills = new Map([
    ["full-feature-workflow", fullFeatureSkillMarkdown()],
    ["product-brief", productBriefSkillMarkdown()],
    ["plan-reviewer", planReviewerSkillMarkdown()],
    ["architecture-reviewer", architectureReviewerSkillMarkdown()],
    ["push-watch", pushWatchSkillMarkdown()],
  ]);
  for (const [name, content] of generatedSkills) {
    writeManagedFile(path.join(AF_DIR, "skills", name, "SKILL.md"), content);
  }
}

function fullFeatureSkillMarkdown() {
  return `---\nname: full-feature-workflow\ndescription: Use this skill for feature work in this project.\n---\n\n# Full Feature Workflow\n\nUse this skill for feature work in this project.\n\nAlways drive progress through the runner output. Run \`${AGENT_FLOW_COMMAND} status\`, then execute the printed \`next_command\` exactly.\n\nDo not skip phases. If existing docs satisfy a phase, write the required artifact and reference those docs. If a gate, review, PR comment, or PR check fails, complete the matching fix phase and push again before merge/handoff.\n\nApply \`code-generation-discipline\` during code and review phases. Resolve required skills from active profile metadata, installed skill index, changed files, and task scope before writing or judging code.\n`;
}

function productBriefSkillMarkdown() {
  return `---\nname: product-brief\ndescription: Use during the full-feature product-brief phase.\n---\n\n# Product Brief\n\nUse during the full-feature product-brief phase.\n\nAsk YC-style forcing questions before implementation:\n\n1. Demand Reality: what behavior proves people want this?\n2. Status Quo: how do they solve it today?\n3. Desperate Specificity: who is the most painful target user?\n4. Narrowest Wedge: what is the smallest version worth using now?\n5. Observation: what concrete user behavior was observed?\n6. Future Fit: why is now the right time?\n\nArtifact template:\n\n# Product Brief\n\n## Mode\nstartup | builder | internal\n\n## Demand Evidence\n\n## Status Quo\n\n## Target User\n\n## Narrowest Wedge\n\n## Observed Behavior\n\n## Why Now\n\n## Cut List\n\n## Assignment\n\n## Decision\nbuild | defer | cut\n`;
}

function planReviewerSkillMarkdown() {
  return `---\nname: plan-reviewer\ndescription: Use during the full-feature plan-review phase.\n---\n\n# Plan Reviewer\n\nUse during the full-feature plan-review phase.\n\nReview only. Do not rewrite the plan.\n\nCheck:\n\n- Missing data collection steps.\n- Missing validation steps.\n- Wrong implementation order.\n- Oversized slices that should be split.\n- Missing state/storage steps.\n- Test coverage gaps.\n- Architecture risks before coding.\n\nArtifact template:\n\n# Plan Review\n\nverdict: approve | request-changes\n\n## Scope Checked\n\n## Missing Steps\n\n## Wrong Order\n\n## Oversized Slices\n\n## Validation Gaps\n\n## Data/State Gaps\n\n## Architecture Risks\n\n## Required Changes\n\n## Approval Notes\n`;
}

function architectureReviewerSkillMarkdown() {
  return `---\nname: architecture-reviewer\ndescription: Use during the full-feature architecture-review phase.\n---\n\n# Architecture Reviewer\n\nUse during the full-feature architecture-review phase.\n\nReview implemented code against domain decisions and DDD/Clean Architecture. Run two independent active-host reviewer sub-agents before approve. Each reviewer section must include \`reviewer-source: sub-agent\`; optional cross-host reviewers are extra evidence and do not replace active-host reviewers.\n\nArtifact template:\n\n# Architecture Review\n\n## Reviewer 1\nreviewer-source: sub-agent\nverdict: approve | request-changes\n\n## Findings\n\n## Domain Alignment\n\n## Layer Violations\n\n## Repository Boundary Issues\n\n## Dependency Direction Issues\n\n## Required Refactors\n\n## Approved Exceptions\n\n## Reviewer 2\nreviewer-source: sub-agent\nverdict: approve | request-changes\n\n## Findings\n\n## Overall\nverdict: approve | request-changes\n\n## Completion Gate\nskills_checked: true\nprofile-skill-selection: applied\nactive-profiles: <profile list>\nchanged-file-skill-resolution: applied\nrequired-profile-skills: checked\nmissing-required-profile-skills: none|<list>\narchitecture-contract-check: pass|fail|n/a\ncodex-claude-parity-check: pass|fail\nhook-parity-check: pass|fail\nclean-architecture: applied\nproject-local-skills: checked|n/a\nproject-local-skills-used: <skill list or n/a>\ndependency-rule: pass|fail\nusecase-boundary: pass|fail|n/a\nusecase-calls-usecase: pass|fail\nrepository-boundary: pass|fail\ncache-boundary: pass|fail|n/a\nmemory-disk-cache-separated: pass|fail|n/a\nmapping-boundary: pass|fail|n/a\ndto-entity-domain-ui-separated: pass|fail\nsolid-boundary-check: pass|fail\npresentation-skill: android|react|react-native|ios|n/a\npresentation-state-review: pass|fail|n/a\nui-state-modeling: explicit|n/a\npresentation-mapping-boundary: domain-to-uimodel|n/a\ndi-boundary: hilt|context-provider|tsyringe|swift-environment|factory|swift-dependencies|swinject|needle|direct|existing|n/a\n`;
}

function pushWatchSkillMarkdown() {
  return `---\nname: push-watch\ndescription: Use this skill after local verification is complete and the branch is ready to publish.\n---\n\n# Push Watch\n\nUse this skill after local verification is complete and the branch is ready to publish.\n\nRun:\n\n\`\`\`bash\n${AGENT_FLOW_COMMAND} run push-watch\n\`\`\`\n\nFlow:\n\n1. Sanity check the branch and working tree.\n2. Commit and push the current branch.\n3. Open or record the pull request.\n4. Watch PR checks and review threads.\n5. Route failures through \`pr-comment-fix\` or \`pr-ci-fix\`; comment fixes must also resolve the corresponding GitHub review threads.\n6. Push again and return to \`pr-watch\`.\n7. When checks and comments are green, route to \`merge\`.\n\nRules:\n\n- Protected branches are blocked: main, master, develop.\n- Record PR watch state with \`status: green\`, \`status: comments\`, \`status: ci-failed\`, or \`status: pending\`.\n- merge requires explicit approval. Do not merge unattended.\n`;
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

function runKitInstall() {
  // kit.mjs가 prompts/rules/bootstrap/concise-output의 canonical generator다.
  // 여기서 먼저 실행하지 않으면 assertInstalled가 요구하는 파일이 빠진다.
  // 단, kit.mjs는 yaml 가능한 python이 필요하므로 없는 환경에서는 경고 후 계속한다.
  const kitCli = path.join(path.dirname(fileURLToPath(import.meta.url)), "agent-flow-kit.mjs");
  const forwarded = INSTALL_ARGS.filter((arg) => arg !== "install");
  const args = [kitCli, "install", ...forwarded];
  const result = spawnSync(process.execPath, args, { cwd: PROJECT, encoding: "utf8" });
  if (result.status !== 0) {
    const detail = (result.stderr || result.stdout || String(result.error || "unknown error")).trim().split("\n")[0];
    console.error(`warning: agent-flow-kit install skipped (${detail}); .agent-flow prompts/bootstrap may be incomplete until \`agent-flow-kit install\` succeeds`);
    return false;
  }
  return true;
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
  runKitInstall();
  ensureDir(path.join(AF_DIR, "runs"));
  ensureDir(path.join(AF_DIR, "memory"));
  ensureDir(path.join(AF_DIR, "memory", "lore"));
  ensureDir(path.join(AF_DIR, "memory", "lore", "archive"));
  ensureDir(path.join(AF_DIR, "local-skills"));

  const profile = detectProfile();
  const previousSkillIndex = readJsonIfExists(path.join(AF_DIR, "skills", "index.json"));
  let installSelection = resolveInstallSelection({ args: INSTALL_ARGS, detectedProfile: profile, kitRoot: KIT_ROOT, projectRoot: PROJECT });
  installSelection = mergeInstallSelectionWithPrevious(installSelection, previousSkillIndex, KIT_ROOT, PROJECT);

  bootstrapMarkdown("CLAUDE.md");
  bootstrapMarkdown("AGENTS.md");
  const gitignorePath = path.join(PROJECT, ".gitignore");
  upsertGitignore(gitignorePath, [
    ".agent-flow/",
    ".agent-flow/local-skills/",
    ".codex/",
    ".Codex/",
    ".claude/",
    ".omp/",
    "AGENTS.md",
    "CLAUDE.md",
    "AGENTS/",
    "CLAUDE/",
    "agent-flow/",
  ]);
  removeGitignoreEntries(gitignorePath, [
    "scripts/check-context-docs.*",
    "graphify/",
    "graphify-out/manifest.json",
    "graphify-out/cost.json",
  ]);
  removeLegacyProjectSkillCopies(PROJECT, "graphify");
  writeManagedFile(
    path.join(AF_DIR, "workflows", "full-feature.yaml"),
    fs.readFileSync(path.join(KIT_ROOT, "workflows", "full-feature.yaml"), "utf8"),
  );

  // Copy bundled skills into project-local skills dir.
  // Host-AI-specific skill paths (`.claude/skills/`, `.Codex/skills/`, `.omp/skills/`) are
  // populated by symlinking from .agent-flow/skills/ where possible, so
  // updates to the kit propagate without re-installing.
  const skillsCopied = copyDir(
    path.join(KIT_ROOT, "skills"),
    path.join(AF_DIR, "skills"),
    PROFILE_MANAGED_HOST_ONLY_SKILLS,
    true,
    FORCE_MANAGED,
    FORCE_MANAGED,
    new Set(["index.json", ...GENERATED_PROJECT_SKILL_NAMES]),
    installSelection.copyRootNames,
  );
  installManagedWorkflowSkills();
  const skillIndex = installProjectSkills(FORCE_MANAGED, installSelection);
  const workflowsCopied = copyDir(
    path.join(KIT_ROOT, "workflows"),
    path.join(AF_DIR, "workflows"),
    new Set(),
    true,
    true,
    true,
  );
  const profilesCopied = copyDir(
    path.join(KIT_ROOT, "profiles"),
    path.join(AF_DIR, "profiles"),
    new Set(),
    true,
    FORCE_MANAGED,
  );
  const templatesCopied = copyDir(
    path.join(KIT_ROOT, "templates"),
    path.join(AF_DIR, "templates"),
    new Set(),
    true,
    FORCE_MANAGED,
    FORCE_MANAGED,
  );
  const scriptsCopied = copyDir(
    path.join(KIT_ROOT, "scripts"),
    path.join(AF_DIR, "scripts"),
    new Set(),
    true,
    FORCE_MANAGED,
  );
  removeStaleContextDocsScripts(AF_DIR, FORCE_MANAGED);
  if (!samePath(PROJECT, KIT_ROOT)) {
    removeDirIfSame(path.join(KIT_ROOT, "scripts"), path.join(PROJECT, "scripts"), FORCE_MANAGED);
  }
  makeHooksExecutable(PROJECT);
  const codexHooksCopied = installCodexHooks(PROJECT);
  installClaudeHooks(PROJECT);
  const ompHooksCopied = installOmpHooks(PROJECT);
  const codexAgentsCopied = copyDir(
    path.join(KIT_ROOT, ".Codex", "agents"),
    path.join(PROJECT, ".Codex", "agents"),
    new Set(),
    true,
    FORCE_MANAGED,
  );
  const claudeAgentsCopied = copyDir(
    path.join(KIT_ROOT, ".claude", "agents"),
    path.join(PROJECT, ".claude", "agents"),
    new Set(),
    true,
    FORCE_MANAGED,
  );
  const contextRulesCopied = copyDir(
    path.join(KIT_ROOT, ".Codex", "rules", "context"),
    path.join(PROJECT, ".Codex", "rules", "context"),
    new Set(),
    true,
    FORCE_MANAGED,
  );
  const contextTreeCopied = copyDir(
    path.join(KIT_ROOT, ".Codex", "context"),
    path.join(PROJECT, ".Codex", "context"),
    new Set(),
    true,
    FORCE_MANAGED,
  );
  copyFileIfMissingOrSame(
    path.join(KIT_ROOT, ".Codex", "rules", "codebase-rubric.md"),
    path.join(PROJECT, ".Codex", "rules", "codebase-rubric.md"),
    FORCE_MANAGED,
  );

  const agentFlowSkill = path.join(AF_DIR, "skills", "agent-flow");
  const claudeSkillStatus = linkOrCopyDir(
    agentFlowSkill,
    path.join(PROJECT, ".claude", "skills", "agent-flow"),
  );
  const codexSkillStatus = linkOrCopyDir(
    agentFlowSkill,
    path.join(hostSkillRoot("codex"), "agent-flow"),
  );
  const ompSkillStatus = linkOrCopyDir(
    agentFlowSkill,
    path.join(hostSkillRoot("omp"), "agent-flow"),
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
    install_scope: "project",
    profile,
    profiles: installSelection.profiles,
    selected_skills: installSelection.skillNames ? [...installSelection.skillNames].sort() : "all",
    project_root: PROJECT,
    installed_at: new Date().toISOString(),
    skills_copied: skillsCopied,
    workflows_copied: workflowsCopied,
    profiles_copied: profilesCopied,
    templates_copied: templatesCopied,
    codex_agents_copied: codexAgentsCopied,
    claude_agents_copied: claudeAgentsCopied,
    codex_hooks_copied: codexHooksCopied,
    omp_hooks_copied: ompHooksCopied,
    context_tree_copied: contextTreeCopied,
    skill_links: {
      claude: claudeSkillStatus,
      codex: codexSkillStatus,
      omp: ompSkillStatus,
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
  console.log(`  omp     : agent-flow skill ${ompSkillStatus}`);
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
    path.join(projectRoot, ".omp", "skills"),
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
  console.log("Usage: npx <agent-flow-package> install [--force-managed]");
  process.exit(0);
} else {
  console.error(`Unknown command: ${cmd}`);
  process.exit(1);
}
