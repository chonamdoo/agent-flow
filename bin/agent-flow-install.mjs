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
import { OMP_EXTENSION_MARKER, ompHooksExtensionSource } from "../lib/omp-hooks-extension.mjs";
import { MANAGED_HOOK_SCRIPTS, RETIRED_MANAGED_HOOK_SCRIPTS } from "../lib/managed-hooks.mjs";
import {
  activeInstallProfileIds,
  AGENT_FLOW_COMMAND,
  architectureReviewerSkillMarkdown,
  arrayValue,
  backupIfDifferent,
  claudeHooksSettings,
  codexConfigPath,
  codexHooksSettings,
  COMMAND_TOOL_MATCHER,
  ensureChildPath,
  escapeRegex,
  fullFeatureSkillMarkdown,
  hasChildWithSuffix,
  HOME,
  hookScriptCommand,
  installedProfileFileNames,
  isPruneBackupName,
  isRetiredHookCommand,
  KIT_ROOT,
  makeHooksExecutable,
  managedHookDigests,
  managedHookScriptName,
  mergeHookConfig,
  mergeHookSettings,
  nextFreeBackupPath,
  ompExtensionIsKitOwned,
  planReviewerSkillMarkdown,
  productBriefSkillMarkdown,
  PRUNE_BACKUP_SUFFIX,
  PRUNE_BACKUP_VERSIONED,
  PRUNE_NOTICE_PREFIX,
  pruneRetiredHooks,
  pruneRetiredHookScripts,
  pruneRetiredManagedScripts,
  pruneUninstalledProfiles,
  pushWatchSkillMarkdown,
  READ_TOOL_MATCHER,
  readHookSettings,
  readJsonIfExists,
  removeCodexBroadTrustState,
  removeGitignoreEntries,
  removeLegacyProjectSkillCopies,
  removeOmpHooksExtension,
  retiredHookScripts,
  safeSkillName,
  shellQuote,
  SKILL_INDEX_END,
  SKILL_INDEX_START,
  skillIndexBlock,
  skillRequires,
  SPEC_PREPARE_TOOL_MATCHER,
  tomlBasicString,
  uniqueStrings,
  unquoteShellWord,
  upsertGitignore,
  upsertSkillIndexBlock,
  validateSkillDependencies,
  writePruneBackup,
} from "../lib/installer-shared.mjs";

// 정의의 정본은 패키지 안이다. `agent-flow-kit.mjs`와 같은 자리를 본다.
const PACKAGED_ASSETS = path.join(KIT_ROOT, "src", "agent_flow");
const INSTALL_ARGS = process.argv.slice(3);
const FORCE_MANAGED = INSTALL_ARGS.includes("--force-managed");
const HOOKS_FLAG_OFF = INSTALL_ARGS.includes("--no-hooks");
const HOOKS_FLAG_ON = INSTALL_ARGS.includes("--hooks");
let hooksDisabled = HOOKS_FLAG_OFF;
// 이 표식이 남아 있으면 OMP 확장 파일을 kit 소유로 보고 업그레이드한다.
const REQUESTED_PROJECT = process.cwd();
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


// 설치된 skill 목록을 AGENTS.md 안에 직접 심는다.
//
// 이전에는 `.agent-flow/skills/index.json`을 읽으라고 안내만 했다. 그건 판단
// 지점이고, agent는 그 판단을 자주 건너뛴다 - Vercel의 Next.js 16 eval에서
// on-demand 문서 조회는 56%의 경우 아예 발동하지 않아 문서 없는 baseline과
// 같은 점수(53%)였고, 같은 문서를 AGENTS.md 인덱스로 심자 100%가 됐다.
//
// 그래서 여기 있는 것은 내용이 아니라 **인덱스**다. 이름만 주고 본문은 파일에
// 남긴다 - 전문을 넣으면 AGENTS.md가 곧 문서가 되고, 적용 시점 문장은 phase
// 프롬프트가 이미 profile YAML에서 그대로 들고 온다. 여기서 다시 지으면 갈라진다.
// 인덱스는 install이 skill 링크를 다 만든 **뒤에** 채운다. bootstrap 블록을 쓰는
// 시점에는 아직 목록이 확정되지 않아, 거기서 채우면 한 install 안에서 곧바로
// 낡는다. 그래서 블록에는 자리만 두고 여기서 그 자리만 바꾼다.

function bootstrapMarkdown(label) {
  const tmplPath = path.join(KIT_ROOT, "bootstrap", `${label}.template`);
  if (!fs.existsSync(tmplPath)) {
    return;
  }
  let block = fs.readFileSync(tmplPath, "utf8");
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

function bootstrapLocalSkillName(skillPath, fallback) {
  try {
    const frontmatter = splitSkillFrontmatter(fs.readFileSync(skillPath, "utf8"));
    if (frontmatter) {
      const parsed = String(parseSimpleYaml(frontmatter).name || "").trim();
      if (parsed) {
        return parsed;
      }
    }
  } catch (_error) {
    // fall through to the directory name
  }
  return fallback;
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



function copyDir(
  src,
  dest,
  excludedRootDirs = new Set(),
  isRoot = true,
  force = false,
  pruneExtraneous = false,
  preservedExtraneousRootNames = new Set(),
  allowedRootDirs = null,
  backupPrunedRoot = null,
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
        backupPrunedRoot,
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
      if (sourceNames.has(entry.name) || (isRoot && preservedExtraneousRootNames.has(entry.name))) {
        continue;
      }
      const target = path.join(dest, entry.name);
      if (backupPrunedRoot) {
        // 백업까지 prune하면 다음 install이 그것을 지운다. 그러면 복구 사본은
        // 재설치 한 번만 버틴다.
        if (isPruneBackupName(entry.name)) {
          continue;
        }
        if (entry.isFile()) {
          const backup = writePruneBackup(target);
          console.log(
            `${PRUNE_NOTICE_PREFIX}${path.relative(backupPrunedRoot, target)}` +
              ` (backup: ${path.relative(backupPrunedRoot, backup)})`,
          );
        }
      }
      fs.rmSync(target, { recursive: true, force: true });
    }
  }
  return { written, skipped };
}


// prune은 source에 없는 파일을 지운다. 사용자가 직접 만든 workflow가 여기
// 걸리면 경고도 사본도 없이 사라졌다. 같은 내용이 이미 백업돼 있으면 다시
// 쓰지 않는다 — 재설치마다 사본이 불어나면 그것대로 잃는 것과 같다.

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







// kit.mjs와 같은 계약: 은퇴한 hook을 기존 settings에서 걷어낸다. 안 그러면 사라진
// 스크립트를 host가 계속 실행해 셸이 막힌다.
// 관측 hook(`record-*`)은 PostToolUse, 강제 hook은 PreToolUse. 이 구분을 어기면
// 관측자가 판정자로 승격되고, 스크립트가 없거나 죽는 순간 host가 그걸 차단으로
// 읽어 사용자 도구가 통째로 막힌다. 런 시작 시 `core/hook_integrity.py`가
// 이 목록과 배치를 kit.json 기록과 대조한다.


// hook을 끄면 "관리 대상 전부를 은퇴시킨 것"과 같다. kit.mjs와 같은 계약이다.














function installCodexHooks(root) {
  const settingsPaths = [
    path.join(root, ".Codex", "hooks.json"),
    path.join(root, ".codex", "hooks.json"),
  ];
  const settings = {};
  for (const settingsPath of settingsPaths) {
    mergeHookConfig(settings, readHookSettings(settingsPath), hooksDisabled);
  }
  mergeHookSettings(settings, codexHooksSettings(root).hooks, hooksDisabled);
  for (const settingsPath of settingsPaths) {
    ensureDir(path.dirname(settingsPath));
    fs.writeFileSync(settingsPath, `${JSON.stringify(settings, null, 2)}\n`, "utf8");
  }
  return true;
}


function installClaudeHooks(root) {
  const settingsPath = path.join(root, ".claude", "settings.json");
  const settings = readHookSettings(settingsPath);
  mergeHookSettings(settings, claudeHooksSettings(root).hooks, hooksDisabled);
  ensureDir(path.dirname(settingsPath));
  fs.writeFileSync(settingsPath, `${JSON.stringify(settings, null, 2)}\n`, "utf8");
}



function upgradeBundledProfiles(root, src, dest, keepNames) {
  if (!fs.existsSync(src)) {
    return { written: 0, skipped: 0, pruned: 0 };
  }
  ensureDir(dest);
  let written = 0;
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    if (!entry.isFile() || !keepNames.has(entry.name)) {
      continue;
    }
    const content = fs.readFileSync(path.join(src, entry.name), "utf8");
    const target = path.join(dest, entry.name);
    backupIfDifferent(root, target, content);
    fs.writeFileSync(target, content, "utf8");
    written += 1;
  }
  // 지운 수를 `skipped`에 실으면 "그대로 둔 파일"과 구분이 안 된다. 배너와
  // kit.json이 그 값을 그대로 보여 주므로 별 필드로 센다.
  const pruned = pruneUninstalledProfiles(root, src, dest, keepNames);
  return { written, skipped: 0, pruned };
}






function installOmpHooks(root) {
  // 다른 host의 hook 설정은 병합이라 업그레이드가 저절로 되지만, 이 확장은
  // 통째로 kit이 만든 파일이다. "내용이 다르면 덮지 않는다"만 걸어 두면
  // 업그레이드가 정의상 내용이 다른 경우라 영구히 고착된다.
  const target = path.join(root, ".omp", "extensions", "agent-flow-hooks.ts");
  if (!ompExtensionIsKitOwned(target) && !FORCE_MANAGED) {
    console.warn(
      `agent-flow: ${path.relative(root, target)} is not kit-managed; ` +
        "leaving it alone. Re-run with --force-managed to replace it.",
    );
    return false;
  }
  // kit 소유여도 사용자가 손댔을 수 있다. 서명만으로는 구분이 안 되므로
  // 다른 내용이면 지우기 전에 사본을 남긴다.
  backupIfDifferent(root, target, ompHooksExtensionSource());
  return writeFileIfMissingOrSame(target, ompHooksExtensionSource(), true);
}

function pruneManagedHookRegistrations(root) {
  for (const rel of [
    [".claude", "settings.json"],
    [".Codex", "hooks.json"],
    [".codex", "hooks.json"],
  ]) {
    const target = path.join(root, ...rel);
    if (!fs.existsSync(target)) {
      continue;
    }
    let settings;
    try {
      settings = JSON.parse(fs.readFileSync(target, "utf8"));
    } catch {
      continue;
    }
    if (pruneRetiredHooks(settings, false, hooksDisabled)) {
      fs.writeFileSync(target, `${JSON.stringify(settings, null, 2)}\n`, "utf8");
      console.log(`  - hooks disabled: cleared ${path.join(...rel)}`);
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
      delivery: metadata.delivery,
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
    // 전달 방식. `passive`는 "항상 적용되는 규범"이라 AGENTS.md 인덱스의 always 줄에
    // 오르고, 나머지는 phase나 사용자 요청이 부를 때만 열린다. 선언이 없으면
    // on-demand다 — 안 쓰는 skill을 상시 노출하면 잡음이 되어 결과가 나빠진다.
    delivery: String(metadata.delivery || "on-demand"),
    excludes: arrayValue(metadata.excludes || metadata.conflicts),
    warnings,
  };
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
  // `existsSync`는 심링크를 **따라가서** 끊어진 링크에 false를 준다. 그러면 stale
  // link 정리 분기를 못 타고 곧바로 링크 생성이 EEXIST로 죽는다. profile을 좁히거나
  // `--skills` 선택을 바꾸면 이전 선택의 host 링크가 끊긴 채 남으므로 실제로 밟는다.
  // lstat은 링크 자체를 보므로 끊어진 링크도 정리 대상으로 잡힌다.
  const stat = lstatIfExists(destDir);
  if (stat) {
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







function previousSkillHash(previousIndex, name) {
  if (!previousIndex || !Array.isArray(previousIndex.skills)) return "";
  return previousIndex.skills.find((skill) => skill && skill.name === name)?.hash || "";
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
  // kit.mjs의 stdout은 여기서 갇힌다. prune 알림만은 사용자가 잃은 파일과
  // 복구 사본을 아는 유일한 통로라 그대로 다시 낸다.
  for (const line of (result.stdout || "").split("\n")) {
    if (line.startsWith(PRUNE_NOTICE_PREFIX)) {
      console.log(line);
    }
  }
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
    "graphify/",
    "scripts/check-context-docs.*",
    "graphify-out/manifest.json",
    "graphify-out/cost.json",
  ]);
  removeLegacyProjectSkillCopies(PROJECT, "graphify");
  writeManagedFile(
    path.join(AF_DIR, "workflows", "full-feature.yaml"),
    fs.readFileSync(path.join(PACKAGED_ASSETS, "workflows", "full-feature.yaml"), "utf8"),
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
  upsertSkillIndexBlock(PROJECT);
  const workflowsCopied = copyDir(
    path.join(PACKAGED_ASSETS, "workflows"),
    path.join(AF_DIR, "workflows"),
    new Set(),
    true,
    true,
    true,
    new Set(),
    null,
    PROJECT,
  );
  // kit이 배포하는 profile은 갱신한다. 사용자 편집을 보호한다고 두면 새 kit이
  // 추가한 필드(skill_sources 등)가 기존 설치본에 영영 안 닿는다.
  // 덮기 전에는 사본을 남긴다.
  //
  // 깔리는 것은 이 프로젝트의 stack + generic + _schema뿐이다. 전부 깔면 남의
  // stack 정의가 함께 쌓여서 어느 파일이 읽히는지 알 수 없다.
  const installedProfileNames = installedProfileFileNames(
    activeInstallProfileIds(profile, installSelection),
    path.join(PACKAGED_ASSETS, "profiles"),
  );
  const profilesCopied = upgradeBundledProfiles(
    PROJECT,
    path.join(PACKAGED_ASSETS, "profiles"),
    path.join(AF_DIR, "profiles"),
    installedProfileNames,
  );
  // runner가 실제로 읽는 자리는 runtime 패키지 사본이다. 보통은 자식
  // `runKitInstall()`이 이미 좁혀 두지만 그 호출은 실패해도 경고만 내고 넘어가므로,
  // 예전 설치본이 남긴 남의 stack profile이 그대로 살아 있을 수 있다.
  pruneUninstalledProfiles(
    PROJECT,
    path.join(PACKAGED_ASSETS, "profiles"),
    path.join(AF_DIR, "runtime", "python", "agent_flow", "profiles"),
    installedProfileNames,
  );
  const templatesCopied = copyDir(
    path.join(KIT_ROOT, "templates"),
    path.join(AF_DIR, "templates"),
    new Set(),
    true,
    FORCE_MANAGED,
    FORCE_MANAGED,
  );
  copyDir(
    path.join(KIT_ROOT, "templates"),
    path.join(AF_DIR, "runtime", "python", "agent_flow", "templates"),
    new Set(),
    true,
    true,
    true,
  );
  const scriptsCopied = copyDir(
    path.join(KIT_ROOT, "scripts"),
    path.join(AF_DIR, "scripts"),
    new Set(),
    true,
    FORCE_MANAGED,
  );
  if (!samePath(PROJECT, KIT_ROOT)) {
    removeDirIfSame(path.join(KIT_ROOT, "scripts"), path.join(PROJECT, "scripts"), FORCE_MANAGED);
  }
  hooksDisabled = HOOKS_FLAG_OFF || (!HOOKS_FLAG_ON && readJsonIfExists(path.join(AF_DIR, "kit.json"))?.hooks === false);
  pruneRetiredHookScripts(PROJECT, hooksDisabled);
  pruneRetiredManagedScripts(PROJECT);
  makeHooksExecutable(PROJECT);
  let codexHooksCopied = false;
  let ompHooksCopied = false;
  removeCodexBroadTrustState(PROJECT);
  if (hooksDisabled) {
    pruneManagedHookRegistrations(PROJECT);
    removeOmpHooksExtension(PROJECT);
  } else {
    codexHooksCopied = installCodexHooks(PROJECT);
    installClaudeHooks(PROJECT);
    ompHooksCopied = installOmpHooks(PROJECT);
  }
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

  // 이 파일이 kit.mjs가 쓴 kit.json을 덮는다. 먼저 읽지 않으면 최초 설치
  // 시각이 재설치마다 지금으로 리셋된다.
  const existingKit = readJsonIfExists(path.join(AF_DIR, "kit.json"));
  const installTimestamp = new Date().toISOString();
  const kitJson = {
    kit: "agent-flow",
    version: "0.1.0",
    install_scope: "project",
    profile,
    profiles: installSelection.profiles,
    selected_skills: installSelection.skillNames ? [...installSelection.skillNames].sort() : "all",
    // 이 파일이 kit.mjs가 쓴 kit.json을 덮는다. 여기 안 남기면 hook 비활성이
    // 재설치마다 풀린다.
    hooks: !hooksDisabled,
    project_root: PROJECT,
    // installed_at은 최초 설치 시각이다. 매 install이 덮으면 "언제부터 쓰던
    // 프로젝트인가"에 답할 기록이 사라진다. 마지막 install은 updated_at이 센다.
    installed_at: typeof existingKit?.installed_at === "string" ? existingKit.installed_at : installTimestamp,
    updated_at: installTimestamp,
    kit_source_digest: existingKit?.kit_source_digest,
    managed_hook_digests: managedHookDigests(),
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
  console.log(`  profiles : ${profilesCopied.written} written, ${profilesCopied.pruned} pruned`);
  console.log(`  claude  : agent-flow skill ${claudeSkillStatus}`);
  console.log(`  codex   : agent-flow skill ${codexSkillStatus}`);
  console.log(`  omp     : agent-flow skill ${ompSkillStatus}`);
  console.log(``);
  console.log(`Next: /agent-flow <task>`);
  console.log(`      (or: agent-flow run "<task>")`);
  console.log(`(If 'agent-flow' isn't on PATH yet: pip install -e ${KIT_ROOT})`);
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
