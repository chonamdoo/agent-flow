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
import { mergeInstallSelectionWithPrevious, resolveInstallSelection } from "../lib/skill-selection.mjs";
import { OMP_EXTENSION_MARKER, ompHooksExtensionSource } from "../lib/omp-hooks-extension.mjs";
import { MANAGED_HOOK_SCRIPTS, RETIRED_MANAGED_HOOK_SCRIPTS } from "../lib/managed-hooks.mjs";
import { parseSimpleYaml, splitFrontmatter } from "../lib/frontmatter.mjs";
import {
  activeInstallProfileIds,
  AGENT_FLOW_COMMAND,
  ASSET_BACKUP_NOTICE_PREFIX,
  ASSET_UPGRADE_NOTICE_PREFIX,
  arrayValue,
  assertInstallRootIsFinal,
  assertKnownInstallArgs,
  atomicWriteFileSync,
  backupIfDifferent,
  BOOTSTRAP_TEMPLATE_FILE,
  claudeHooksSettings,
  codexConfigPath,
  codexHooksSettings,
  COMMAND_TOOL_MATCHER,
  ensureChildPath,
  escapeRegex,
  hasChildWithSuffix,
  hookScriptCommand,
  hookLauncherDigest,
  INSTALL_SYNOPSIS,
  installHelpRequested,
  installedProfileFileNames,
  installProjectLauncher,
  installHookLauncher,
  syncManagedWorktreeHostHooks,
  isRecordedKitAsset,
  isPruneBackupName,
  isRetiredHookCommand,
  KIT_ASSETS_RELATIVE,
  KIT_ROOT,
  makeHooksExecutable,
  managedHookDigests,
  managedHookScriptName,
  mergeHookConfig,
  mergeHookSettings,
  nextFreeBackupPath,
  ompExtensionIsKitOwned,
  preserveKitSkillHashes,
  projectLauncherDigest,
  projectLauncherPythonRecord,
  PRUNE_BACKUP_SUFFIX,
  PRUNE_BACKUP_VERSIONED,
  PRUNE_NOTICE_PREFIX,
  pruneRetiredHooks,
  pruneRetiredHookScripts,
  pruneRetiredManagedScripts,
  BOOTSTRAP_ADOPTED_NOTICE_PREFIX,
  BOOTSTRAP_KEPT_NOTICE_PREFIX,
  isSymlinkPath,
  pruneUninstalledProfiles,
  pathHasSymlink,
  READ_TOOL_MATCHER,
  readHookSettings,
  readJsonIfExists,
  readKitAssetRecord,
  removeCodexBroadTrustState,
  removeGitignoreEntries,
  removeLegacyProjectSkillCopies,
  removeOmpHooksExtension,
  reportSkippedUserEdit,
  requestedInstallRootOption,
  resolveManagedWorktreeRoot,
  resolveLinkedWorktreeLeader,
  ROOT_CONTEXT_FILES,
  rootBootstrapBlock,
  resolveInstallRoot,
  retiredHookScripts,
  safeSkillName,
  reportRootBootstrapBlocks,
  samePath,
  shellQuote,
  SKILL_INDEX_END,
  SKILL_INDEX_START,
  SKILL_UPGRADE_NOTICE_PREFIX,
  skillIndexBlock,
  SYMLINK_FOLLOW_NOTICE_PREFIX,
  SYMLINK_SKIP_NOTICE_PREFIX,
  syncRecordedKitAssets,
  tomlBasicString,
  uniqueStrings,
  unquoteShellWord,
  upgradeBundledSkills,
  upsertGitExclude,
  upsertGitignore,
  upsertDocsIndexBlock,
  upsertSkillIndexBlock,
  upsertRootBootstrapBlock,
  validateSkillDependencies,
  writeKitAssetRecord,
  withoutInstallRootOption,
  writePruneBackup,
} from "../lib/installer-shared.mjs";

// 정의의 정본은 패키지 안이다. `agent-flow-kit.mjs`와 같은 자리를 본다.
const PACKAGED_ASSETS = path.join(KIT_ROOT, "src", "agent_flow");
const INSTALL_ARGS = process.argv.slice(3);
const FORCE_MANAGED = INSTALL_ARGS.includes("--force-managed");
const HOOKS_FLAG_OFF = INSTALL_ARGS.includes("--no-hooks");
const HOOKS_FLAG_ON = INSTALL_ARGS.includes("--hooks");
let hooksDisabled = HOOKS_FLAG_OFF;
// `--root` 오류는 메시지 한 줄로 끝낸다. 모듈 최상단에서 그냥 throw하면 사용자가
// 오타 하나에 스택 트레이스를 받는다.
function requestedProject() {
  try {
    const requested = requestedInstallRootOption(INSTALL_ARGS, process.cwd());
    if (requested === undefined) {
      return process.cwd();
    }
    assertInstallRootIsFinal(requested, resolveInstallRoot(requested));
    return requested;
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exit(1);
  }
}

// `resolveInstallRoot`는 git이 대답을 못 주면 throw한다. 이 상수는 모듈 평가 중에
// 계산되므로 파일 끝 dispatch의 try/catch가 받지 못한다 - 감싸지 않으면 모든 명령이
// 생 Node 스택 트레이스로 죽는다(`--help`까지).
function installRoot(requested) {
  try {
    return resolveInstallRoot(requested);
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exit(1);
  }
}

// help는 질문이다. 아래 두 상수가 모듈 평가 중에 root를 풀고 `process.exit(1)`로
// 끝날 수 있으므로 그보다 먼저 답한다 — `install --root <오타> --help`가 usage 대신
// root 오류로 죽으면 `--help`는 안전한 질문이 아니게 된다.
if (process.argv[2] === "install" && installHelpRequested(INSTALL_ARGS)) {
  console.log(`usage: npx <agent-flow-package> ${INSTALL_SYNOPSIS}`);
  process.exit(0);
}

// `--root`는 install만 받는다. 커맨드 디스패치보다 먼저 도는 자리라, 여기서
// 무조건 해석하면 `bogus --root /nope`가 `Unknown command`가 아니라 root 오류로 죽는다.
const REQUESTED_PROJECT = process.argv[2] === "install" ? requestedProject() : process.cwd();
// `--root`와 같은 이유로 install에서만 푼다. 모든 명령이 계산하면 git이 못 도는
// 환경에서 `--help`와 `Unknown command`까지 git 오류로 죽는다. `AF_DIR`은 install
// 밖에서 쓰이지 않는다.
const PROJECT = process.argv[2] === "install" ? installRoot(REQUESTED_PROJECT) : REQUESTED_PROJECT;
const AF_DIR = path.join(PROJECT, ".agent-flow");

const PROJECT_SKILL_HOSTS = Object.freeze(["claude", "codex", "omp"]);
const BUNDLED_HOST_SKILL_NAMES = new Set([
  "agent-flow",
  "agent-flow-diagnosing-bugs",
  "app-shell-error-contract",
  "android-appshell-error-handling",
  "comment-authoring-discipline",
  "comment-checker",
  "ios-app-shell-error-handling",
  "react-app-shell-error-handling",
  "react-native-app-shell-error-handling",
]);
// 설치된 외부 skill 이름은 여기 열거하지 않는다. upstream이 6개월에 이름 35%를 바꿨고
// (실측: `camera1-to-camerax` → `camerax`), 열거된 목록은 우리가 배포하지도 않는 이름을
// 영구히 들고 있게 된다. 프로젝트 skill 색인은 우리가 배포한 것만 담고, 외부 skill은
// 런타임이 host 경로에서 해석한다.
const PROFILE_MANAGED_HOST_ONLY_SKILLS = new Set();

function ensureDir(p) {
  fs.mkdirSync(p, { recursive: true });
}




// linked worktree 판정. leader를 cwd와 직접 비교하면 `<leader>/src`처럼 leader의
// 하위 디렉토리에서 install하는 정상 경로까지 막힌다 - 그래서 이 checkout의
// toplevel과 견준다. linked worktree에서만 toplevel(worktree root)과
// leader(git common dir의 부모)가 갈라진다.





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

// 정본은 `bootstrap/AGENTS.md.template` 한 벌이고, `agent-flow-kit.mjs`도 같은 파일을 읽는다.
// label이 고르는 것은 어느 루트 파일에 쓰는지와, `rootBootstrapBlock`이 CLAUDE.md에는 계약
// 본문 대신 `@AGENTS.md` 포인터를 낸다는 것뿐이다. 못 읽으면 던진다 — 조용히 건너뛰면
// "블록을 안 쓴 것"이 정상 결과가 되고, 이전 install이 남긴 낡은 블록이 그대로 방치된다.
//
// 쓰기와 소유권 판정은 `upsertRootBootstrapBlock` 한 벌이다. 두 진입점이 각자 판정하면
// 어느 CLI로 깔았는지에 따라 사용자 편집이 보존되기도 하고 지워지기도 한다.
function bootstrapMarkdown(label) {
  const tmplPath = path.join(KIT_ROOT, "bootstrap", BOOTSTRAP_TEMPLATE_FILE);
  let template;
  try {
    template = fs.readFileSync(tmplPath, "utf8");
  } catch (error) {
    throw new Error(`bootstrap template unreadable: ${tmplPath} (${error?.message || error})`);
  }
  // `rootBootstrapBlock`은 try 밖이다. 마커가 없다는 진단이 "읽을 수 없다"로 바뀌면
  // 고칠 곳을 찾는 사람이 파일 권한을 보러 간다.
  return upsertRootBootstrapBlock(PROJECT, label, rootBootstrapBlock(label, template), {
    force: FORCE_MANAGED,
  });
}

function bootstrapLocalSkillName(skillPath, fallback) {
  try {
    const frontmatter = splitFrontmatter(fs.readFileSync(skillPath, "utf8"));
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

// `flutter create`가 `dependencies:`에 쓰는 Flutter SDK 의존. pubspec을 가진 순수 Dart
// 패키지와 Flutter 저장소를 가르는 유일한 표지다. Python 쪽과 같은 패턴을 쓴다 — 인라인
// 주석을 허용하고, `#` 앞의 공백을 요구해 `sdk: flutter#c` 스칼라는 잡지 않는다.
const FLUTTER_SDK_DEPENDENCY_RE = /^\s*sdk:\s*["']?flutter["']?(?:[ \t]+#.*)?[ \t]*$/m;

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
  // `pubspec.yaml`만으로는 Flutter가 아니다 — 순수 Dart 패키지도 전부 갖고 있고, 그런
  // 저장소를 flutter로 잡으면 `flutter analyze`·`flutter test`가 상시 실패하는 필수
  // gate가 된다. 확정 표지는 `flutter create`가 쓰는 SDK 의존이다. gradle 분기보다 앞에
  // 두는 이유는 Android 호스트를 함께 빌드하는 monorepo다. 값으로 매칭한다 — YAML
  // 스칼라라 인용과 공백이 저자의 선택이고, 바이트 비교는 `sdk: "flutter"`를 놓친다.
  const pubspecPath = path.join(PROJECT, "pubspec.yaml");
  if (fs.existsSync(pubspecPath) && FLUTTER_SDK_DEPENDENCY_RE.test(fs.readFileSync(pubspecPath, "utf8"))) {
    return "flutter";
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
    if (isSymlinkPath(destPath)) {
      console.log(`${SYMLINK_SKIP_NOTICE_PREFIX}${destPath}`);
      skipped += 1;
      continue;
    }
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
          // 기록이 있는 자산은 record sync가 판정한다. 여기서도 보고하면 곧
          // 갱신될 파일에 "user-modified"가 붙어 출력이 서로를 부정한다.
          const label = path.relative(PROJECT, destPath);
          if (!isRecordedKitAsset(label)) {
            reportSkippedUserEdit(label);
          }
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
    const label = path.relative(PROJECT, dest);
    if (!isRecordedKitAsset(label)) {
      reportSkippedUserEdit(label);
    }
    return false;
  }
  fs.copyFileSync(src, dest);
  return true;
}

function writeFileIfMissingOrSame(dest, content, force = false) {
  ensureDir(path.dirname(dest));
  if (force) {
    atomicWriteFileSync(dest, content);
    return true;
  }
  if (fs.existsSync(dest) && fs.readFileSync(dest, "utf8") !== content) {
    reportSkippedUserEdit(path.relative(PROJECT, dest));
    return false;
  }
  atomicWriteFileSync(dest, content);
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
    // host가 읽는 사용자 설정이다. 공용 dotfile로 심링크해 둔 프로젝트가 있어
    // 링크를 따라간다(정책은 `atomicWriteFileSync` 주석).
    atomicWriteFileSync(settingsPath, `${JSON.stringify(settings, null, 2)}\n`, { followSymlink: true });
  }
  return true;
}


function installClaudeHooks(root) {
  const settingsPath = path.join(root, ".claude", "settings.json");
  const settings = readHookSettings(settingsPath);
  mergeHookSettings(settings, claudeHooksSettings(root).hooks, hooksDisabled);
  // host가 읽는 사용자 설정이다. 링크를 따라간다.
  atomicWriteFileSync(settingsPath, `${JSON.stringify(settings, null, 2)}\n`, { followSymlink: true });
}



// managed hook은 digest가 `kit.json`에 기록되고 run 시작이 그 일치를 요구한다
// (`hook_integrity`). "내용이 다르면 덮지 않는다"만 걸어 두면 kit 업그레이드가
// 정의상 내용이 다른 경우라 digest는 새 값, 파일은 옛 값으로 갈라져 run이 막힌다.
function upgradeManagedHooks(root, src, dest) {
  if (!fs.existsSync(src)) {
    return;
  }
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    if (!entry.isFile()) {
      continue;
    }
    const source = path.join(src, entry.name);
    const content = fs.readFileSync(source, "utf8");
    const target = path.join(dest, entry.name);
    const { safeToWrite, backup } = backupIfDifferent(root, target, content);
    // 사본을 못 남겼으면 덮지 않는다. 옛 판본으로 남은 hook은 `hook_integrity`가
    // run 시작에서 잡아 주고 다음 install이 다시 시도하지만, 사본 없이 덮은
    // 사용자 편집은 되돌릴 방법이 없다.
    if (!safeToWrite) {
      continue;
    }
    if (backup !== null) {
      // 백업은 hook 디렉터리 안에 남는다. 실행 권한을 그대로 물려주면
      // `hook_integrity`가 관리 대상 아닌 실행 파일로 보고 run 시작을 막는다.
      fs.chmodSync(backup, 0o644);
    }
    atomicWriteFileSync(target, content, { mode: fs.statSync(source).mode & 0o777 });
  }
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
    // 사본을 못 남겼으면 덮지 않는다. 사용자가 고친 profile을 사본 없이 잃는 것보다
    // 옛 판본으로 남는 편이 낫다 - 다음 install이 다시 시도한다.
    if (!backupIfDifferent(root, target, content).safeToWrite) {
      continue;
    }
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
  // kit 소유여도 사용자가 손댔을 수 있다. 서명만으로는 구분이 안 되므로 다른 내용이면
  // 덮기 전에 사본을 남기고, 사본을 못 남겼으면 덮지 않는다 - 확장이 옛 판본으로
  // 남는 것보다 사용자 편집을 사본 없이 잃는 것이 나쁘다.
  if (!backupIfDifferent(root, target, ompHooksExtensionSource()).safeToWrite) {
    return false;
  }
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
      // 위 세 경로는 전부 host가 읽는 사용자 설정이다. 링크를 따라간다.
      atomicWriteFileSync(target, `${JSON.stringify(settings, null, 2)}\n`, { followSymlink: true });
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
  const index = preserveKitSkillHashes({ ...selected, links }, previousIndex, path.join(KIT_ROOT, "skills"));
  // 잘린 index.json은 `readJsonIfExists`가 조용히 삼켜 "설치된 skill 없음"으로
  // 읽힌다. 그러면 다음 install이 관리 링크를 stale로 보고 걷어낸다.
  atomicWriteFileSync(path.join(AF_DIR, "skills", "index.json"), `${JSON.stringify(index, null, 2)}\n`);
  return index;
}

function selectProjectSkills(installSelection = null) {
  const discovered = [
    ...discoverSkills(path.join(AF_DIR, "local-skills"), "local", PROFILE_MANAGED_HOST_ONLY_SKILLS),
    ...discoverProjectSkills(),
    ...discoverSkills(path.join(AF_DIR, "skills"), "bundled", new Set(["index.json", "catalog.lock.json", ...PROFILE_MANAGED_HOST_ONLY_SKILLS])),
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
  const frontmatter = splitFrontmatter(text);
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
  const body = text.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/, "");
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
    requires: uniqueStrings([...arrayValue(metadata.dependencies), ...arrayValue(metadata.requires)]),
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








function previousSkillHash(previousIndex, name) {
  if (!previousIndex || !Array.isArray(previousIndex.skills)) return "";
  return previousIndex.skills.find((skill) => skill && skill.name === name)?.hash || "";
}





function runKitInstall() {
  // kit.mjs가 prompts/rules/bootstrap/concise-output의 canonical generator다.
  // 여기서 먼저 실행하지 않으면 assertInstalled가 요구하는 파일이 빠진다.
  // 단, kit.mjs는 yaml 가능한 python이 필요하므로 없는 환경에서는 경고 후 계속한다.
  const kitCli = path.join(path.dirname(fileURLToPath(import.meta.url)), "agent-flow-kit.mjs");
  // 자식은 이미 해석된 PROJECT를 cwd로 받는다. 상대 `--root`를 그대로 넘기면
  // 자식이 제 cwd 기준으로 한 번 더 풀어 다른 곳을 가리킨다.
  const forwarded = withoutInstallRootOption(INSTALL_ARGS).filter((arg) => arg !== "install");
  const args = [kitCli, "install", ...forwarded];
  const result = spawnSync(process.execPath, args, { cwd: PROJECT, encoding: "utf8" });
  // kit.mjs의 stdout은 여기서 갇힌다. prune과 skill 갱신 알림만은 사용자가 무엇이
  // 바뀌었는지 아는 유일한 통로라 그대로 다시 낸다 — 실제 갱신은 자식이 하므로
  // 여기서 걸러 내면 install.mjs 경로가 통째로 무음이 된다.
  for (const line of (result.stdout || "").split("\n")) {
    if (
      line.startsWith(PRUNE_NOTICE_PREFIX)
      || line.startsWith(SKILL_UPGRADE_NOTICE_PREFIX)
      || line.startsWith(ASSET_UPGRADE_NOTICE_PREFIX)
      || line.startsWith(ASSET_BACKUP_NOTICE_PREFIX)
      || line.startsWith(BOOTSTRAP_KEPT_NOTICE_PREFIX)
      || line.startsWith(BOOTSTRAP_ADOPTED_NOTICE_PREFIX)
      // 링크 너머로 쓴 host 설정. 자식이 쓰고 여기서 걸러 내면 프로젝트 밖 파일을
      // 갈아 끼운 사실이 어디에도 안 남는다.
      || line.startsWith(SYMLINK_FOLLOW_NOTICE_PREFIX)
    ) {
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
  // linked worktree(Orca의 `~/orca/workspaces/<repo>/<slug>` 등)도 managed 경로와
  // 똑같이 fail-closed다. 조용히 leader를 PROJECT로 잡으면 이 아래 전부가 leader를
  // 때린다: `bootstrapMarkdown`이 leader의 CLAUDE.md/AGENTS.md를 백업 없이 덮고,
  // tracked `.gitignore`를 고치고, `.claude/settings.json`과
  // `.agent-flow/profiles/*`(미선택 profile 삭제)를 갈아치우며, `--force-managed`면
  // `removeDirIfSame`가 tracked `<leader>/scripts/`를 내용 확인 없이 recursive 삭제한다.
  const leaderRoot = resolveLinkedWorktreeLeader(REQUESTED_PROJECT);
  if (leaderRoot) {
    // 종료코드는 managed 분기와 같아야 한다. 같은 정책인데 rc만 갈라지면
    // `npx agent-flow install`을 CI/부트스트랩 스크립트에 넣은 사용자가 worktree
    // 안에서만 스크립트 전진이 죽는다. leader에 설치본이 있으면 "이미 설치됨"이라
    // 할 일이 없는 것이지 실패가 아니다.
    if (fs.existsSync(path.join(leaderRoot, ".agent-flow", "kit.json"))) {
      console.log(`agent-flow already installed root=${leaderRoot}`);
      console.log("worktree install skipped; reinstall from the leader checkout if needed");
      return;
    }
    console.error(`linked worktree install blocked; install from the leader checkout ${leaderRoot}`);
    process.exitCode = 1;
    return;
  }
  // 자식 kit install이 index를 다시 쓴다. 그 뒤에 읽으면 "사용자가 손댔는가"를
  // 가르는 hash가 방금 관측한 현재 내용으로 갱신돼 있어 오라클이 사라진다.
  const previousSkillIndex = readJsonIfExists(path.join(AF_DIR, "skills", "index.json"));
  const delegatedKitInstalled = runKitInstall();
  ensureDir(path.join(AF_DIR, "runs"));
  ensureDir(path.join(AF_DIR, "memory"));
  ensureDir(path.join(AF_DIR, "local-skills"));

  const profile = detectProfile();
  let installSelection = resolveInstallSelection({ args: INSTALL_ARGS, detectedProfile: profile, kitRoot: KIT_ROOT, projectRoot: PROJECT });
  installSelection = mergeInstallSelectionWithPrevious(installSelection, previousSkillIndex, KIT_ROOT, PROJECT);

  // 자식 kit install이 이미 두 루트 파일을 썼고 receipt까지 남겼다. 여기서 한 번 더
  // 쓰면 방금 관측한 내용이 곧 "우리가 쓴 것"으로 기록돼, 사용자가 손댔는지 가르는
  // 오라클이 사라진다 — 실제로 그 두 번째 쓰기가 `--force-managed` 없이도 편집을
  // 덮었다. 자식이 실패했을 때만(= kit 자산이 안 깔린 degraded 설치) 직접 쓴다.
  if (!delegatedKitInstalled) {
    reportRootBootstrapBlocks(ROOT_CONTEXT_FILES.map((label) => bootstrapMarkdown(label)));
  }
  const gitignorePath = path.join(PROJECT, ".gitignore");
  upsertGitignore(gitignorePath, [
    ".agent-flow/",
    ".agent-flow/local-skills/",
    ".codex/",
    ".Codex/",
    ".claude/",
    ".omp/",
    // 루트 `AGENTS.md`/`CLAUDE.md`는 여기 올리지 않는다. 무엇을 커밋할지는 프로젝트가
    // 정하고, 툴이 ignore로 밀어 넣으면 그 파일은 clone과 linked worktree에서 사라져
    // 그쪽 세션이 계약을 못 받는다. 이미 적혀 있는 항목은 지우지 않는다 — 그건
    // 프로젝트가 내린 결정이고, 되돌리는 것도 프로젝트 몫이다.
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
  upsertGitExclude(PROJECT, ROOT_CONTEXT_FILES);
  removeLegacyProjectSkillCopies(PROJECT, "graphify");
  writeManagedFile(
    path.join(AF_DIR, "workflows", "full-feature.yaml"),
    fs.readFileSync(path.join(PACKAGED_ASSETS, "workflows", "full-feature.yaml"), "utf8"),
  );

  // Copy bundled skills into project-local skills dir.
  // Host-AI-specific skill paths (`.claude/skills/`, `.Codex/skills/`, `.omp/skills/`) are
  // populated by symlinking from .agent-flow/skills/ where possible, so
  // updates to the kit propagate without re-installing.
  const recordedAssets = readKitAssetRecord(PROJECT);
  const writtenAssets = new Map();
  upgradeBundledSkills(
    PROJECT,
    path.join(KIT_ROOT, "skills"),
    path.join(AF_DIR, "skills"),
    previousSkillIndex,
    installSelection.copyRootNames,
    new Set(),
  );
  const skillsCopied = copyDir(
    path.join(KIT_ROOT, "skills"),
    path.join(AF_DIR, "skills"),
    PROFILE_MANAGED_HOST_ONLY_SKILLS,
    true,
    FORCE_MANAGED,
    FORCE_MANAGED,
    new Set(["index.json", "catalog.lock.json"]),
    installSelection.copyRootNames,
  );
  const skillIndex = installProjectSkills(FORCE_MANAGED, installSelection);
  upsertSkillIndexBlock(PROJECT);
  upsertDocsIndexBlock(PROJECT);
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
  // 여기 깔리는 것은 이 프로젝트가 실제로 쓰는 stack + generic + _schema뿐이다.
  // runtime 패키지 사본(`runtime/python/agent_flow/profiles/`)은 좁히지 않는다 —
  // 그쪽이 실제 read path이자 override가 참조할 카탈로그다(`agent-flow-kit.mjs` 참조).
  const installedProfileNames = installedProfileFileNames(
    activeInstallProfileIds(profile, installSelection),
  );
  const profilesCopied = upgradeBundledProfiles(
    PROJECT,
    path.join(PACKAGED_ASSETS, "profiles"),
    path.join(AF_DIR, "profiles"),
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
  upgradeManagedHooks(
    PROJECT,
    path.join(KIT_ROOT, "scripts", "hooks"),
    path.join(AF_DIR, "scripts", "hooks"),
  );
  if (!samePath(PROJECT, KIT_ROOT)) {
    removeDirIfSame(path.join(KIT_ROOT, "scripts"), path.join(PROJECT, "scripts"), FORCE_MANAGED);
  }
  hooksDisabled = HOOKS_FLAG_OFF || (!HOOKS_FLAG_ON && readJsonIfExists(path.join(AF_DIR, "kit.json"))?.hooks === false);
  pruneRetiredHookScripts(PROJECT, hooksDisabled);
  pruneRetiredManagedScripts(PROJECT);
  makeHooksExecutable(PROJECT);
  // kit.mjs 쪽 install이 실패해도(PyYAML 없는 환경) 이 진입점이 kit.json을 덮는다.
  // launcher를 여기서 한 번 더 보장하지 않으면 그 조합에서 승인 경로만 조용히 죽는다.
  installProjectLauncher(PROJECT);
  installHookLauncher(PROJECT);
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
  // 복사 단계는 내용이 다르면 손대지 않으므로 kit이 고친 자산이 기존 설치본에
  // 닿지 않는다. `SKILL.md`는 index hash가, managed hook은 digest가 오라클이고,
  // 나머지는 이 기록이다. 목록은 `RECORDED_KIT_ASSET_TREES` 한 벌이다.
  //
  // 복사가 전부 끝난 뒤에 돈다. 앞서 돌면 이번 install이 처음 만든 파일이 기록에
  // 안 남아, 다음 install이 근거 없이 판정한다.
  if (recordedAssets) {
    syncRecordedKitAssets(PROJECT, KIT_ROOT, recordedAssets, writtenAssets);
    writeKitAssetRecord(PROJECT, writtenAssets);
  } else {
    console.warn(`warning: ${KIT_ASSETS_RELATIVE} is unreadable; kit asset sync skipped (delete it to re-bootstrap)`);
  }

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
    project_launcher_digest: projectLauncherDigest(PROJECT),
    hook_launcher_digest: hookLauncherDigest(PROJECT),
    project_launcher_python: projectLauncherPythonRecord(),
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
  atomicWriteFileSync(path.join(AF_DIR, "kit.json"), JSON.stringify(kitJson, null, 2));
  if (delegatedKitInstalled) {
    syncManagedWorktreeHostHooks(PROJECT);
  }

  console.log(`agent-flow installed`);
  console.log(`  profile : ${profile}`);
  // 두 진입점이 같은 `root` 라벨로 다른 것을 냈다 - 여기는 `.agent-flow` 디렉토리,
  // kit은 프로젝트 루트. 확인하고 싶은 것은 "leader에 깔렸나 worktree에 깔렸나"라
  // 프로젝트 루트 쪽으로 맞춘다.
  console.log(`  root    : ${PROJECT}`);
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
  // 여기서 던지는 것 중 사용자가 고칠 수 있는 것이 있다 - 대표적으로 managed 경로
  // 하나가 끊어진 symlink인 경우다. 맨몸으로 두면 그 안내가 stack trace 안에 묻힌다.
  // `agent-flow-kit.mjs`의 진입점과 같은 계약이다.
  try {
    assertKnownInstallArgs(INSTALL_ARGS);
    install();
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exit(1);
  }
} else if (cmd === "--help" || cmd === "-h" || !cmd) {
  console.log(`usage: npx <agent-flow-package> ${INSTALL_SYNOPSIS}`);
  process.exit(0);
} else {
  console.error(`Unknown command: ${cmd}`);
  process.exit(1);
}
