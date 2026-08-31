#!/usr/bin/env node

import fs from "node:fs";
import crypto from "node:crypto";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { spawnSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { fileURLToPath } from "node:url";
import { mergeInstallSelectionWithPrevious, resolveInstallSelection } from "../lib/skill-selection.mjs";
import { OMP_EXTENSION_MARKER, ompHooksExtensionSource } from "../lib/omp-hooks-extension.mjs";
import { MANAGED_HOOK_SCRIPTS, RETIRED_MANAGED_HOOK_SCRIPTS } from "../lib/managed-hooks.mjs";
import { observeSkillContent, parseSkillMetadata } from "../lib/skill-metadata.mjs";
import {
  activeInstallProfileIds,
  AGENT_FLOW_COMMAND,
  assertInstallRootIsFinal,
  assertKnownInstallArgs,
  atomicWriteFileSync,
  backupIfDifferent,
  BOOTSTRAP_TEMPLATE_FILE,
  claudeHooksSettings,
  canonicalPath,
  cliOptionValue,
  codexConfigPath,
  codexHooksSettings,
  COMMAND_TOOL_MATCHER,
  ensureChildPath,
  escapeRegex,
  extractCliOption,
  gitOutput,
  gitEnv,
  hasChildWithSuffix,
  hookScriptCommand,
  hookLauncherDigest,
  INSTALL_SYNOPSIS,
  installHelpRequested,
  installedProfileFileNames,
  installProjectLauncher,
  installHookLauncher,
  syncManagedWorktreeHostHooks,
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
  isSymlinkPath,
  pruneRetiredHookScripts,
  pruneRetiredManagedScripts,
  pruneUninstalledProfiles,
  READ_TOOL_MATCHER,
  pathHasSymlink,
  readHookSettings,
  readJsonIfExists,
  readKitAssetRecord,
  removeCodexBroadTrustState,
  removeGitignoreEntries,
  removeLegacyProjectSkillCopies,
  removeOmpHooksExtension,
  requestedInstallRootOption,
  resolveManagedWorktreeRoot,
  resolveManagedWorktreeContext,
  resolveLinkedWorktreeLeader,
  resolveInstallRoot,
  resolveGitCommonWorktreeRoot,
  ROOT_CONTEXT_FILES,
  rootBootstrapBlock,
  retiredHookScripts,
  samePath,
  shellQuote,
  reportRootBootstrapBlocks,
  SKILL_INDEX_END,
  SKILL_INDEX_START,
  skillIndexBlock,
  SYMLINK_SKIP_NOTICE_PREFIX,
  syncRecordedKitAssets,
  tomlBasicString,
  unquoteShellWord,
  upsertGitExclude,
  upgradeBundledSkills,
  upsertGitignore,
  upsertDocsIndexBlock,
  upsertSkillIndexBlock,
  upsertRootBootstrapBlock,
  validateSkillDependencies,
  writeKitAssetRecord,
  writePruneBackup,
} from "../lib/installer-shared.mjs";

const command = process.argv[2];
// 워크플로·프로파일 정의의 정본은 설치 가능한 패키지 안이다. 루트에 사본을 두면
// 같은 파일이 두 벌이 되고, 둘이 갈라지지 않았는지 보는 검사가 따로 필요해진다.
const PACKAGED_ASSETS = path.join(KIT_ROOT, "src", "agent_flow");
const RUNTIME_PYTHON_RELATIVE = path.join(".agent-flow", "runtime", "python");
const installArgs = process.argv.slice(3);
const forceManaged = installArgs.includes("--force-managed");
// hook 비활성은 프로젝트 설정이다. 한 번 끄면 재설치가 되살리지 않는다.
// 다시 켜려면 `install --hooks`.
const hooksFlagOff = installArgs.includes("--no-hooks");
const hooksFlagOn = installArgs.includes("--hooks");
let hooksDisabled = hooksFlagOff;
// 이 표식이 남아 있으면 OMP 확장 파일을 kit 소유로 보고 업그레이드한다.
let cachedFullFeatureWorkflow = null;
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

function requestedInstallRoot() {
  const requested = requestedInstallRootOption(installArgs, process.cwd());
  if (requested === undefined) {
    return process.cwd();
  }
  assertInstallRootIsFinal(requested, resolveInstallRoot(requested));
  return requested;
}

function installProject(requestedRoot) {
  const managedWorktreeRoot = resolveManagedWorktreeRoot(requestedRoot);
  if (
    managedWorktreeRoot
  ) {
    if (fs.existsSync(path.join(managedWorktreeRoot, ".agent-flow", "kit.json"))) {
      console.log(`agent-flow already installed root=${managedWorktreeRoot}`);
      console.log("worktree install skipped; reinstall from the leader checkout if needed");
    } else {
      throw new Error("managed worktree install blocked; install from the leader checkout first");
    }
    return;
  }
  // linked worktree(Orca의 `~/orca/workspaces/<repo>/<slug>` 등)도 managed worktree와
  // 같은 정책이다. `resolveInstallRoot`가 leader로 올라가 버리면 다른 checkout에서
  // leader의 tracked 파일(bootstrap markdown, .gitignore, profiles)을 갈아치운다.
  const linkedLeader = resolveLinkedWorktreeLeader(requestedRoot);
  if (linkedLeader) {
    // managed 분기와 같은 종료코드 정책이다(leader에 설치본이 있으면 skip + rc 0).
    // 갈라지면 install을 CI/부트스트랩에 넣은 사용자가 worktree 안에서만 죽는다.
    if (fs.existsSync(path.join(linkedLeader, ".agent-flow", "kit.json"))) {
      console.log(`agent-flow already installed root=${linkedLeader}`);
      console.log("worktree install skipped; reinstall from the leader checkout if needed");
    } else {
      throw new Error("linked worktree install blocked; install from the leader checkout first");
    }
    return;
  }
  const root = resolveInstallRoot(requestedRoot);
  const agentFlowDir = path.join(root, ".agent-flow");
  const profile = detectProfile(root);
  let installSelection = resolveInstallSelection({ args: installArgs, detectedProfile: profile, kitRoot: KIT_ROOT, projectRoot: root });
  const existingPayload = readExistingKit(agentFlowDir);
  // 명시 플래그 > 이전 설정 > 기본(켜짐)
  hooksDisabled = hooksFlagOff || (!hooksFlagOn && existingPayload?.hooks === false);
  const previousSkillIndex = readJsonIfExists(path.join(agentFlowDir, "skills", "index.json"));
  installSelection = mergeInstallSelectionWithPrevious(installSelection, previousSkillIndex, KIT_ROOT, root);
  const phases = fullFeaturePhases();

  for (const name of ["runs", "state", "handoffs", "team", "worktrees", "workflows", "skills", "templates", "prompts", "rules", "bootstrap"]) {
    fs.mkdirSync(path.join(agentFlowDir, name), { recursive: true });
  }
  fs.mkdirSync(path.join(agentFlowDir, "local-skills"), { recursive: true });

  fs.mkdirSync(path.join(agentFlowDir, "skills", "agent-flow"), { recursive: true });
  fs.mkdirSync(path.join(agentFlowDir, "skills", "full-feature-workflow"), { recursive: true });

  // digest는 심은 파일에서 뽑으므로 payload보다 먼저 심는다.
  installProjectLauncher(root);
  installHookLauncher(root);
  const installTimestamp = new Date().toISOString();
  const payload = {
    install_scope: "project",
    profile,
    profiles: installSelection.profiles,
    selected_skills: installSelection.skillNames ? [...installSelection.skillNames].sort() : "all",
    root: ".",
    hooks: !hooksDisabled,
    // installed_at은 최초 설치 시각이다. 매 install이 덮으면 "언제부터 쓰던
    // 프로젝트인가"에 답할 기록이 사라진다. 마지막 install은 updated_at이 센다.
    installed_at: typeof existingPayload?.installed_at === "string" ? existingPayload.installed_at : installTimestamp,
    updated_at: installTimestamp,
    // 설치본이 어느 kit source에서 왔는지. 이 값이 없으면 낡은 설치본이 조용히
    // 계속 돈다 — version은 하드코딩이라 릴리스 없이 바뀐 자산을 구분하지 못한다.
    kit_source_digest: kitSourceDigest(),
    managed_hook_digests: managedHookDigests(),
    project_launcher_digest: projectLauncherDigest(root),
    hook_launcher_digest: hookLauncherDigest(root),
    project_launcher_python: projectLauncherPythonRecord(),
  };

  writeManagedFile(path.join(agentFlowDir, "workflows", "full-feature.yaml"), fullFeatureWorkflowYaml());
  copyBundledDirIfMissingOrSame(
    path.join(PACKAGED_ASSETS, "workflows"),
    path.join(agentFlowDir, "workflows"),
    true,
    new Set(),
    true,
    true,
    new Set(),
    null,
    root,
  );
  const recordedAssets = readKitAssetRecord(root);
  const writtenAssets = new Map();
  upgradeBundledSkills(
    root,
    path.join(KIT_ROOT, "skills"),
    path.join(agentFlowDir, "skills"),
    previousSkillIndex,
    installSelection.copyRootNames,
    new Set(),
  );
  copyBundledDirIfMissingOrSame(
    path.join(KIT_ROOT, "skills"),
    path.join(agentFlowDir, "skills"),
    forceManaged,
    PROFILE_MANAGED_HOST_ONLY_SKILLS,
    true,
    forceManaged,
    new Set(["index.json", "catalog.lock.json"]),
    installSelection.copyRootNames,
  );
  // kit이 배포하는 profile은 갱신한다. 사용자 편집을 보호한다고 두면 새 kit이
  // 추가한 필드(skill_sources 등)가 기존 설치본에 영영 안 닿는다.
  // 덮기 전에는 사본을 남긴다.
  //
  // 여기 깔리는 것은 이 프로젝트가 실제로 쓰는 stack + generic + _schema뿐이다.
  // 전부 깔면 남의 stack 정의가 함께 쌓여서 어느 파일이 이 프로젝트에 걸리는지
  // 구분할 수 없다.
  const installedProfileNames = installedProfileFileNames(
    activeInstallProfileIds(profile, installSelection),
  );
  upgradeBundledProfiles(
    root,
    path.join(PACKAGED_ASSETS, "profiles"),
    path.join(agentFlowDir, "profiles"),
    installedProfileNames,
  );
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, "templates"), path.join(agentFlowDir, "templates"), forceManaged, new Set(), true, forceManaged);
  const skillIndex = installProjectSkills(root, agentFlowDir, previousSkillIndex, forceManaged, installSelection);
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, "scripts"), path.join(agentFlowDir, "scripts"), forceManaged);
  // scripts 복사는 사용자 편집을 보호해 덮지 않는다. managed hook은 그 보호가 곧
  // digest 불일치이므로 별도로 갱신한다.
  upgradeManagedHooks(
    root,
    path.join(KIT_ROOT, "scripts", "hooks"),
    path.join(agentFlowDir, "scripts", "hooks"),
  );
  // 이 runtime 패키지 사본의 profile은 좁히지 않는다. `find_kit_root()`가 설치본에서
  // 이 디렉터리로 떨어지므로 profile YAML을 실제로 읽는 자리가 여기이고, override는
  // 설치 때 고르지 않은 profile도 지명할 수 있다 — 좁히면 `gates --profile ios`가
  // `unknown profile: ios`로 죽고, `AGENT_FLOW_PROFILE=ios`는 경고만 내고 `generic`
  // 으로 조용히 내려앉는다(`core/profile_resolution.py` `load_single_profile`). 위 `.agent-flow/profiles/`
  // 가 "이 프로젝트의 stack"을 보여 주는 자리이고, 이쪽은 override가 참조할 카탈로그다.
  copyBundledDirIfMissingOrSame(
    path.join(KIT_ROOT, "src", "agent_flow"),
    path.join(root, RUNTIME_PYTHON_RELATIVE, "agent_flow"),
    true,
    new Set(),
    true,
    true,
  );
  copyBundledDirIfMissingOrSame(
    path.join(KIT_ROOT, "templates"),
    path.join(root, RUNTIME_PYTHON_RELATIVE, "agent_flow", "templates"),
    true,
    new Set(),
    true,
    true,
  );
  if (!samePath(root, KIT_ROOT)) {
    removeManagedDirIfSame(path.join(KIT_ROOT, "scripts"), path.join(root, "scripts"), forceManaged);
  }
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, ".Codex", "agents"), path.join(root, ".Codex", "agents"), forceManaged);
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, ".claude", "agents"), path.join(root, ".claude", "agents"), forceManaged);
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, ".Codex", "rules", "context"), path.join(root, ".Codex", "rules", "context"), forceManaged);
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, ".Codex", "context"), path.join(root, ".Codex", "context"), forceManaged);
  removeCodexBroadTrustState(root);
  if (!hooksDisabled) {
    installCodexHooks(root);
  }
  writeManagedFileIfMissingOrSame(
    path.join(root, ".Codex", "rules", "codebase-rubric.md"),
    fs.readFileSync(path.join(KIT_ROOT, ".Codex", "rules", "codebase-rubric.md"), "utf8"),
    forceManaged,
  );
  writeManagedFileIfMissingOrSame(
    path.join(root, ".Codex", "rules", "concise-output.md"),
    fs.readFileSync(path.join(KIT_ROOT, "skills", "agent-flow-concise-output", "concise-output.md"), "utf8"),
    forceManaged,
  );
  // 복사 단계는 내용이 다르면 손대지 않으므로 kit이 고친 자산이 기존 설치본에
  // 닿지 않는다. `SKILL.md`는 index hash가, managed hook은 digest가 오라클이고,
  // 나머지는 이 기록이다. 목록은 `RECORDED_KIT_ASSET_TREES` 한 벌이다.
  //
  // 복사가 전부 끝난 뒤에 돈다. 앞서 돌면 이번 install이 처음 만든 파일이 기록에
  // 안 남고, 그 다음 install은 근거 없이 판정해 사용자 편집을 사본만 남기고 덮는다.
  if (recordedAssets) {
    syncRecordedKitAssets(root, KIT_ROOT, recordedAssets, writtenAssets);
    writeKitAssetRecord(root, writtenAssets);
  } else {
    console.warn(`warning: ${KIT_ASSETS_RELATIVE} is unreadable; kit asset sync skipped (delete it to re-bootstrap)`);
  }
  writeManagedFile(path.join(agentFlowDir, "prompts", "push-watch.md"), pushWatchPromptMarkdown());
  writeManagedFile(path.join(agentFlowDir, "prompts", "push-watch-tick.md"), pushWatchTickPromptMarkdown());
  for (const phase of phases) {
    writeManagedFile(
      path.join(agentFlowDir, "prompts", `${phase.id}.md`),
      phasePrompt(phase),
    );
  }
  writeManagedFile(path.join(agentFlowDir, "rules", "workflow-contract.md"), workflowContract());
  writeManagedFile(path.join(agentFlowDir, "bootstrap", "AGENTS.md"), managedBootstrapMarkdown("AGENTS.md"));
  writeManagedFile(path.join(agentFlowDir, "bootstrap", "CLAUDE.md"), managedBootstrapMarkdown("CLAUDE.md"));
  const gitignorePath = path.join(root, ".gitignore");
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
  upsertGitExclude(root, ROOT_CONTEXT_FILES);
  removeLegacyProjectSkillCopies(root, "graphify");
  // 순서는 `ROOT_CONTEXT_FILES`와 같아야 보고가 어긋나지 않는다. receipt는 각 쓰기가
  // 스스로 남기므로 여기서 따로 모으지 않는다.
  const bootstrapTemplateText = readBootstrapTemplate().text;
  const bootstrapStatuses = ROOT_CONTEXT_FILES.map((label) => upsertRootBootstrapBlock(
    root,
    label,
    rootBootstrapBlock(label, bootstrapTemplateText),
    { force: forceManaged },
  ));
  reportRootBootstrapBlocks(bootstrapStatuses);
  upsertSkillIndexBlock(root);
  upsertDocsIndexBlock(root);
  pruneRetiredHookScripts(root, hooksDisabled);
  pruneRetiredManagedScripts(root);
  makeHooksExecutable(root);
  if (hooksDisabled) {
    // 등록을 지우는 것은 prune이 한다. 여기서는 host별 설정을 한 번 더 훑어
    // 남은 항목을 걷어내고, 통째로 kit이 만든 OMP 확장은 파일째 치운다.
    pruneManagedHookRegistrations(root);
    removeOmpHooksExtension(root);
  } else {
    installClaudeHooks(root);
    installOmpHooks(root);
  }

  payload.skill_index = {
    path: ".agent-flow/skills/index.json",
    skills: skillIndex.skills.length,
    conflicts: skillIndex.conflicts.length,
    warnings: skillIndex.warnings.length,
  };

  atomicWriteFileSync(path.join(agentFlowDir, "kit.json"), `${JSON.stringify(payload, null, 2)}\n`);
  syncManagedWorktreeHostHooks(root);
  syncSkillSources(root);
  // root를 같이 낸다. `--root`를 줬든 cwd에서 유도했든, 어디에 설치됐는지 보이지
  // 않으면 잘못된 checkout에 깔린 것을 알 방법이 없다 - 실제로 leader 대신
  // worktree에 깔린 것을 한참 뒤에야 알아챘다.
  console.log(`agent-flow installed profile=${profile} root=${root}`);
}

// 설치 시점에 1회만 돈다. 런타임에는 절대 호출하지 않는다 — 사용자에게 매번 물어보지 않기 위한 지점이다.
function syncSkillSources(root) {
  const out = runSkillsCli(["sync", "--root", root]);
  if (out === null) {
    return;
  }
  for (const line of out.split("\n")) {
    // 선언이 없으면 조용히 넘어간다. install 출력은 계약이라 노이즈를 추가하지 않는다.
    if (line && !line.endsWith("no skill_sources declared")) {
      console.log(`skills: ${line}`);
    }
  }
}

// Lifecycle state has one owner: the Python CLI. JS only translates the
// compatibility command names and never creates or advances run state.
const PYTHON_RUN_LIFECYCLE = new Set(["start", "status", "next", "advance"]);
const JS_RUN_SUBCOMMAND_NAMES = [
  "start",
  "status",
  "next",
  "advance",
  "push-watch",
  "push-watch-tick",
];

function runWorkflowCommand(args) {
  const subcommand = args[0];
  const requestedRoot = cliOptionValue(args.slice(1), "--root");
  const root = resolveAgentFlowRoot(
    requestedRoot ? path.resolve(process.cwd(), requestedRoot) : process.cwd(),
  );
  warnIfInstalledKitIsStale(root);

  if (PYTHON_RUN_LIFECYCLE.has(subcommand)) {
    assertPythonRuntimeInstalled(root);
    relayPythonRunLifecycle(subcommand, args.slice(1), root);
    return;
  }

  if (subcommand === "push-watch") {
    assertInstalled(root);
    const branch = currentBranch(process.cwd());
    if (["main", "master", "develop"].includes(branch)) {
      throw new Error(`blocked: protected branch ${branch}`);
    }
    const state = {
      status: "watching",
      branch,
      iterations: 0,
      updated_at: new Date().toISOString(),
    };
    writeJson(pushWatchStatePath(root), state);
    console.log(`push-watch watching branch=${branch}`);
    return;
  }

  if (subcommand === "push-watch-tick") {
    assertInstalled(root);
    withPushWatchLock(root, () => {
      const state = readCurrentRun(root);
      if (state.phase !== "pr-watch") {
        throw new Error(`blocked: push-watch-tick requires current phase pr-watch, got ${state.phase}`);
      }
      const runDir = resolveRunDir(root, state.run_dir);
      const pr = readPullRequestStatus(process.cwd());
      const watchStatus = pullRequestWatchStatus(pr);
      commitPushWatchObservation(root, runDir, state.run_id, pr, watchStatus);
      console.log(`push-watch status=${watchStatus}`);
    });
    return;
  }


  // 아는 서브커맨드가 아니면 Python CLI의 `run`이 주인이다. `agent-flow run
  // "<task>"`가 여기로 온다 — 래퍼 자신의 안내문(`readCurrentRun`)이 그 형태를
  // 지시하는데 usage로 죽으면 사용자는 run을 시작조차 못 한다.
  if (!subcommand) {
    throw new Error(
      'usage: agent-flow-kit run <install|start|status|next|advance|push-watch|push-watch-tick> | run "<task>"',
    );
  }
  // 오타는 task가 아니다. Python `run`은 task로 worktree와 브랜치까지 만들므로
  // `run statsu` 한 번이 쓰레기 worktree를 남긴다. 서브커맨드에 근접한 단일 토큰은
  // 넘기지 않는다. 여러 단어면 사람이 쓴 task이므로 그대로 넘긴다.
  // `stats`처럼 정말 그 한 단어가 task인 경우가 있으므로 `--`로 빠져나갈 수 있다.
  if (subcommand === "--") {
    runPythonCliCommand("run", args.slice(1), { interactive: true });
  }
  const nearMiss = args.length === 1 ? nearestRunSubcommand(subcommand) : "";
  if (nearMiss) {
    throw new Error(
      `unknown run subcommand: ${subcommand}. did you mean \`run ${nearMiss}\`? `
      + `if ${subcommand} really is the task, run it as \`agent-flow-kit run -- ${subcommand}\` `
      + `or use the Python CLI directly: agent-flow run "${subcommand}"`,
    );
  }
  runPythonCliCommand("run", args, { interactive: true });
}

function relayPythonRunLifecycle(subcommand, args, root) {
  const requestedWorktree = cliOptionValue(args, "--worktree");
  const worktree = requestedWorktree
    ? resolveRegisteredWorktreeName(root, requestedWorktree)
    : null;
  const checkoutIdentity = worktree
    ? `worktree:${worktree}`
    : currentCheckoutIdentity(root);
  if (checkoutIdentity === "unknown") {
    throw new Error(
      "checkout identity is unknown; refusing to relay lifecycle state",
    );
  }

  const rootedArgs = hasCliOption(args, "--root")
    ? [...args]
    : [...args, "--root", root];
  // `--worktree`를 여기서 만들어 넣지 않는다. 그러면 Python이 명시 selector로 보고
  // 암묵 재사용 동의 게이트를 건너뛴다. cwd 유도는 Python이 같은 규칙으로 다시 한다.
  const identityArgs = [
    ...rootedArgs,
    "--checkout-identity",
    checkoutIdentity,
  ];
  if (subcommand === "start") {
    const extracted = extractCliOption(identityArgs, "--workflow");
    runPythonCliCommand(
      "start",
      [extracted.value ?? "full-feature", ...extracted.args],
      { interactive: true },
    );
    return;
  }
  if (subcommand === "next") {
    runPythonCliCommand("status", identityArgs);
    return;
  }
  runPythonCliCommand(
    subcommand === "advance" ? "continue" : "status",
    identityArgs,
  );
}

function hasCliOption(args, name) {
  return args.some((arg) => arg === name || arg.startsWith(`${name}=`));
}

// `--workflow`와 갈리는 지점까지 좁힌 접두 판정과 짝을 이룬다. 편집 거리 2는
// 인접 키 오타와 글자 누락을 덮고, 실제 task 문구까지 삼키지 않는 폭이다.
function nearestRunSubcommand(token) {
  if (/\s/.test(token) || token.startsWith("-")) {
    return "";
  }
  for (const candidate of ["install", ...JS_RUN_SUBCOMMAND_NAMES]) {
    if (candidate !== token && editDistance(candidate, token) <= 2) {
      return candidate;
    }
  }
  return "";
}

function editDistance(left, right) {
  let previous = Array.from({ length: right.length + 1 }, (_, index) => index);
  for (let i = 1; i <= left.length; i += 1) {
    const current = [i];
    for (let j = 1; j <= right.length; j += 1) {
      current[j] = Math.min(
        previous[j] + 1,
        current[j - 1] + 1,
        previous[j - 1] + (left[i - 1] === right[j - 1] ? 0 : 1),
      );
    }
    previous = current;
  }
  return previous[right.length];
}



function loadWorkflowDefinition(name) {
  if (!/^[A-Za-z0-9_-]+$/.test(name)) {
    throw new Error(`unsafe workflow name: ${name}`);
  }
  const workflowPath = path.join(PACKAGED_ASSETS, "workflows", `${name}.yaml`);
  const text = fs.readFileSync(workflowPath, "utf8");
  const definition = exportWorkflowDefinition(name);
  return {
    id: definition.id,
    text,
    phases: normalizeExportedPhases(definition, name),
  };
}

function fullFeatureWorkflow() {
  if (cachedFullFeatureWorkflow === null) {
    cachedFullFeatureWorkflow = loadWorkflowDefinition("full-feature");
  }
  return cachedFullFeatureWorkflow;
}

function fullFeaturePhases() {
  return fullFeatureWorkflow().phases;
}

function workflowPhases(name) {
  return name === "full-feature" ? fullFeaturePhases() : loadWorkflowDefinition(name).phases;
}

function exportWorkflowDefinition(name) {
  const result = safeSpawnSync(preferredPython(), [
    "-m",
    "agent_flow.cli",
    "workflow",
    "export",
    "--workflow",
    name,
    "--format",
    "json",
  ], {
    cwd: KIT_ROOT,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    env: {
      ...process.env,
      PYTHONPATH: [path.join(KIT_ROOT, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
    },
    timeout: 10_000,
  });
  if (result.error || result.status !== 0) {
    const detail = result.error?.message || result.stderr?.trim() || `exit ${result.status}`;
    throw new Error(`workflow export failed for ${name}: ${detail}`);
  }
  try {
    return JSON.parse(result.stdout);
  } catch (error) {
    throw new Error(`workflow export returned invalid JSON for ${name}: ${error.message}`);
  }
}

function normalizeExportedPhases(definition, name) {
  // Node wrapper는 phase schema를 재해석하지 않고 Python 정규화 결과만 검증한다.
  if (!definition || typeof definition !== "object") {
    throw new Error(`workflow export ${name}: expected object`);
  }
  if (typeof definition.id !== "string" || !definition.id) {
    throw new Error(`workflow export ${name}: missing id`);
  }
  if (!Array.isArray(definition.phases) || definition.phases.length === 0) {
    throw new Error(`workflow export ${name}: missing phases`);
  }
  return definition.phases.map((phase, index) => normalizeExportedPhase(phase, name, index));
}

function normalizeExportedPhase(phase, name, index) {
  if (!phase || typeof phase !== "object") {
    throw new Error(`workflow export ${name}: phase ${index} must be an object`);
  }
  const id = requireExportedString(phase.id, name, index, "id");
  const description = optionalExportedString(phase.description, name, index, "description", "");
  const prompt = phase.prompt === null
    ? null
    : optionalExportedString(phase.prompt, name, index, "prompt", "");
  const artifact = requireExportedString(phase.artifact, name, index, "artifact");
  const requiredMarkers = normalizeExportedStringList(phase.required_markers, name, index, "required_markers");
  return {
    id,
    artifact,
    description,
    instruction: prompt || description,
    required_markers: requiredMarkers,
    multi_review: normalizeExportedBoolean(phase.multi_review, name, index, "multi_review"),
    routes: normalizeExportedRoutes(phase.routes, name, index),
    skills: normalizeExportedSkills(phase.skills, name, index),
  };
}

function normalizeExportedSkills(value, name, index) {
  if (value === null || value === undefined) {
    return null;
  }
  if (typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`workflow export ${name}: phase ${index} skills must be an object`);
  }
  return {
    required: normalizeExportedStringList(value.required, name, index, "skills.required"),
    optional: normalizeExportedStringList(value.optional, name, index, "skills.optional"),
  };
}

function requireExportedString(value, name, index, field) {
  if (typeof value !== "string" || !value) {
    throw new Error(`workflow export ${name}: phase ${index} ${field} must be a non-empty string`);
  }
  return value;
}

function optionalExportedString(value, name, index, field, fallback) {
  if (value === undefined) {
    return fallback;
  }
  if (typeof value !== "string") {
    throw new Error(`workflow export ${name}: phase ${index} ${field} must be a string`);
  }
  return value;
}

function normalizeExportedBoolean(value, name, index, field) {
  if (value === undefined) {
    return false;
  }
  if (typeof value !== "boolean") {
    throw new Error(`workflow export ${name}: phase ${index} ${field} must be a boolean`);
  }
  return value;
}

function normalizeExportedStringList(value, name, index, field) {
  if (value === undefined) {
    return [];
  }
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new Error(`workflow export ${name}: phase ${index} ${field} must be a string array`);
  }
  return value;
}

function normalizeExportedRoutes(value, name, index) {
  if (value === undefined || value === null) {
    return null;
  }
  if (typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`workflow export ${name}: phase ${index} routes must be an object`);
  }
  for (const [key, target] of Object.entries(value)) {
    if (typeof key !== "string" || typeof target !== "string") {
      throw new Error(`workflow export ${name}: phase ${index} routes must map strings to strings`);
    }
  }
  return value;
}

// `flutter create`가 `dependencies:`에 쓰는 Flutter SDK 의존. pubspec을 가진 순수 Dart
// 패키지와 Flutter 저장소를 가르는 유일한 표지다. Python 쪽과 같은 패턴을 쓴다 — 인라인
// 주석을 허용하고, `#` 앞의 공백을 요구해 `sdk: flutter#c` 스칼라는 잡지 않는다.
const FLUTTER_SDK_DEPENDENCY_RE = /^\s*sdk:\s*["']?flutter["']?(?:[ \t]+#.*)?[ \t]*$/m;

function detectProfile(rootDir) {
  // 설치 배너도 Python CLI·install.mjs와 같은 profile을 보여줘야 agent가 다른 guide를 고르지 않는다.
  if (fs.existsSync(path.join(rootDir, "next.config.js")) ||
      fs.existsSync(path.join(rootDir, "next.config.mjs")) ||
      fs.existsSync(path.join(rootDir, "next.config.ts"))) {
    return "nextjs";
  }
  if (
    fs.existsSync(path.join(rootDir, "Package.swift")) ||
    hasChildWithSuffix(rootDir, ".xcodeproj") ||
    hasChildWithSuffix(rootDir, ".xcworkspace")
  ) {
    return "ios";
  }
  if (fs.existsSync(path.join(rootDir, "pyproject.toml")) ||
      fs.existsSync(path.join(rootDir, "requirements.txt"))) {
    return "python";
  }
  const earlyPackagePath = path.join(rootDir, "package.json");
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
  const pubspecPath = path.join(rootDir, "pubspec.yaml");
  if (fs.existsSync(pubspecPath) && FLUTTER_SDK_DEPENDENCY_RE.test(fs.readFileSync(pubspecPath, "utf8"))) {
    return "flutter";
  }
  if (
    fs.existsSync(path.join(rootDir, "build.gradle")) ||
    fs.existsSync(path.join(rootDir, "settings.gradle")) ||
    fs.existsSync(path.join(rootDir, "build.gradle.kts")) ||
    fs.existsSync(path.join(rootDir, "settings.gradle.kts"))
  ) {
    return "android";
  }
  const packagePath = path.join(rootDir, "package.json");
  if (fs.existsSync(packagePath)) {
    const packageText = fs.readFileSync(packagePath, "utf8");
    if (packageText.includes("react-native")) {
      return "react-native";
    }
    if (packageText.includes("\"next\"")) {
      return "nextjs";
    }
    // 일반 TypeScript 프로젝트는 node보다 좁은 profile을 써야 gate와 skill routing이 맞다.
    if (fs.existsSync(path.join(rootDir, "tsconfig.json"))) {
      return "typescript";
    }
    return "node";
  }
  // npm gate를 실행할 수 없는 tsconfig 단독 프로젝트는 generic으로 둔다.
  return "generic";
}



function resolveAgentFlowRoot(start) {
  const worktreeRoot = resolveManagedWorktreeRoot(start);
  if (worktreeRoot && fs.existsSync(path.join(worktreeRoot, ".agent-flow", "kit.json"))) {
    return worktreeRoot;
  }
  const gitCommonRoot = resolveGitCommonWorktreeRoot(start);
  if (gitCommonRoot) {
    return gitCommonRoot;
  }
  const parts = start.split(path.sep);
  const markerIndex = parts.lastIndexOf(".agent-flow");
  if (markerIndex !== -1) {
    const root = parts.slice(0, markerIndex).join(path.sep) || path.sep;
    if (fs.existsSync(path.join(root, ".agent-flow", "kit.json"))) {
      return root;
    }
  }
  let current = start;
  while (true) {
    if (fs.existsSync(path.join(current, ".agent-flow", "kit.json"))) {
      return current;
    }
    const parent = path.dirname(current);
    if (parent === current) {
      return start;
    }
    current = parent;
  }
}







function currentCheckoutIdentity(root) {
  const cwd = canonicalPath(process.cwd());
  const managed = resolveManagedWorktreeContext(cwd);
  if (managed?.name && samePath(managed.root, root)) {
    return `worktree:${managed.name}`;
  }
  // 채택 판정이 leader containment보다 먼저다. 채택된 checkout이 `<leader>/.worktrees/foo`
  // 처럼 leader 디렉터리 **아래**이면서 관리 경로 밖이면, containment가 먼저 "leader"를
  // 내고 Python은 cwd의 채택 기록으로 `worktree:foo`를 유도해 두 값이 어긋난다.
  // 채택 기록은 leader 자신을 지목할 수 없으므로(leader는 채택 대상이 아니다) 이 순서가
  // leader 판정을 삼키지 않는다.
  const adopted = adoptedCheckoutIdentity(root, cwd);
  if (adopted) {
    return adopted;
  }
  const leader = canonicalPath(root);
  const relative = path.relative(leader, cwd);
  if (
    relative === ""
    || (relative !== ".."
      && !relative.startsWith(`..${path.sep}`)
      && !path.isAbsolute(relative))
  ) {
    return "leader";
  }
  return "unknown";
}

function adoptedCheckoutIdentity(root, cwd) {
  const name = adoptedCheckoutName(root, cwd);
  return name ? `worktree:${name}` : null;
}

function adoptedCheckoutName(root, cwd) {
  const topLevel = gitOutput(cwd, ["rev-parse", "--show-toplevel"]);
  const commonDir = gitOutput(cwd, ["rev-parse", "--git-common-dir"]);
  if (!topLevel || !commonDir) {
    return null;
  }
  const common = path.resolve(cwd, commonDir);
  if (path.basename(common) !== ".git" || !samePath(path.dirname(common), root)) {
    return null;
  }
  const checkout = canonicalPath(topLevel);
  const name = path.basename(checkout);
  const record = path.join(common, "agent-flow", "adopted", `${name}.json`);
  let payload;
  try {
    payload = JSON.parse(fs.readFileSync(record, "utf8"));
  } catch {
    return null;
  }
  const recorded = payload && typeof payload.path === "string" ? payload.path : "";
  if (!recorded || !samePath(recorded, checkout)) {
    return null;
  }
  // 경로 일치는 예선일 뿐이다. 채택 기록의 registration fingerprint까지 맞아야
  // 한다 — 지운 뒤 같은 자리에 다시 만든 checkout이 앞 checkout의 runtime state를
  // 물려받으면 `run push-watch`가 남의 artifact를 갱신한다. 판정은 Python이 소유한다.
  const verified = pythonCliOutput(
    "worktree",
    ["identity", "--root", root, "--path", checkout],
    { cwd: checkout },
  );
  return verified === `worktree:${name}` ? name : null;
}


// linked worktree 판정. leader를 cwd와 직접 비교하면 `<leader>/src`처럼 leader의
// 하위 디렉토리에서 install하는 정상 경로까지 막힌다 - 그래서 이 checkout의
// toplevel과 견준다. linked worktree에서만 toplevel(worktree root)과
// leader(git common dir의 부모)가 갈라진다.


const DEFAULT_RELAY_TIMEOUT_MS = 30_000;
// gates는 프로파일 게이트를 순차로 돌린다. 개별 게이트 상한은 Python `--timeout`이
// 소유하므로 wrapper는 그 총량만 감싼다. 무제한으로 두면 python이 시작조차 못 한
// 경우 전경이 영영 멈춘다.
const DEFAULT_GATE_TIMEOUT_S = 600;
// `--profile a,b`와 kit.json의 profiles 리스트는 여러 프로파일의 게이트를 합집합으로
// 돌린다(`src/agent_flow/core/gate_plan.py:profile_gate_commands`). 배포 프로파일 전부를 켠
// 실측 최대가 20개라 그보다 위에서 자른다.
const MAX_TOTAL_GATES = 24;
const GATE_RELAY_SLACK_MS = 120_000;
// `gateTimeoutSeconds * MAX_TOTAL_GATES`는 **모든 gate가 기본 상한**이라고 가정한다.
// `gates[].timeout_s`가 그 가정을 깬다 — 배포 profile 전부를 켠 plan의 선언 합계
// 실측이 15,600초로 기본 예산 14,400초를 넘는다. 넘으면 gate는 다 돌았는데 결과
// 파일을 쓰기 전에 SIGKILL이 나고, 파일이 없으니 cursor가 gates에 남아 다음
// 실행이 같은 자리에서 다시 죽는다. JS가 profile YAML을 읽어 예산을 계산하게
// 만들지는 않는다(parity가 profile 로직의 JS 재구현을 금지한다). 바닥값을 둔다.
// 여백은 `tests/test_cli.py`의 `test_declared_gate_timeouts_fit_inside_the_relay_budget`가
// 지킨다 — 새 profile이 상한을 올리면 그 테스트가 먼저 깨진다.
const GATE_PLAN_FLOOR_S = 18_000;

function requestedGateTimeoutSeconds(args) {
  // argparse는 `--timeout 900`, `--timeout=900`, 유일 접두 축약(`--time 900`)을
  // 모두 받는다. 한 형태만 읽으면 사용자가 올린 상한이 예산에 반영되지 않는다.
  // 지정하지 않았으면 `null`이다 — 기본값으로 접으면 "사용자가 지정했다"와
  // "선언이 실제 상한이다"를 구분할 수 없다.
  const isTimeoutFlag = (value) =>
    value.length >= 3 && "--timeout".startsWith(value);
  let selected = null;
  for (const [index, arg] of args.entries()) {
    if (typeof arg !== "string" || !arg.startsWith("--t")) {
      continue;
    }
    const separator = arg.indexOf("=");
    const flag = separator === -1 ? arg : arg.slice(0, separator);
    if (!isTimeoutFlag(flag)) {
      continue;
    }
    const raw = separator === -1 ? args[index + 1] : arg.slice(separator + 1);
    const requested = Number(raw);
    if (Number.isFinite(requested) && requested > 0) {
      // argparse는 플래그가 반복되면 마지막 값을 쓴다. 첫 값을 잡으면 예산이
      // 실제 게이트 상한보다 작아질 수 있다.
      selected = requested;
    }
  }
  return selected;
}

function gateTimeoutSeconds(args) {
  return requestedGateTimeoutSeconds(args) ?? DEFAULT_GATE_TIMEOUT_S;
}

// architecture-lint는 profile gate로도 돌고(그때는 gate 예산 안에 있다) node로
// 직접도 불린다. 직접 호출만 30초로 잘리면 같은 명령이 호출 경로에 따라 다르게
// 죽는다. Python 파서에 `--timeout`이 없으므로 게이트 하나치 기본 예산을 준다.
const SINGLE_GATE_RELAY_TIMEOUT_MS = DEFAULT_GATE_TIMEOUT_S * 1000 + GATE_RELAY_SLACK_MS;

function gatePlanRelayTimeoutMs(args) {
  // 명시한 상한은 profile 선언을 이긴다(`core/gates.py`의 `run_gate`). 그래서
  // 명시했으면 선언 합계를 볼 이유가 없고, 생략했으면 선언이 실제 상한이라
  // 바닥값이 필요하다. 둘을 `max`로 접으면 바닥값이 사용자가 올린 상한을 삼켜
  // `--timeout`을 올려도 예산이 그대로인 구간이 생긴다.
  const requested = requestedGateTimeoutSeconds(args);
  const seconds =
    requested === null ? GATE_PLAN_FLOOR_S : requested * MAX_TOTAL_GATES;
  return seconds * 1000 + GATE_RELAY_SLACK_MS;
}

function relayTimeoutForSubcommand(subcommand, args) {
  if (subcommand === "gates") {
    return gatePlanRelayTimeoutMs(args);
  }
  if (subcommand === "architecture-lint") {
    return SINGLE_GATE_RELAY_TIMEOUT_MS;
  }
  if (subcommand === "continue") {
    // `continue`는 phase를 전진시키는 명령이고, gates phase에서는 runner가 프로파일
    // 게이트를 **이 프로세스 안에서** 직접 돌린다(`src/agent_flow/runner.py`의
    // `_run_project_gates`). 30초를 주면 android/ios의 gradle·xcodebuild 게이트는
    // 첫 하나도 끝나지 않고, SIGKILL 시점에는 gate-results.json이 아직 없어서
    // cursor가 gates에 그대로 남는다 — 다음 `run advance`가 같은 자리에서 다시
    // 죽어 이 진입점에서 gates를 통과할 방법이 사라진다. multi-review도 같은
    // 프로세스에서 reviewer subprocess를 돌리므로 예산은 게이트 총량과 같이 둔다.
    // 상한을 없애지는 않는다 — python이 시작조차 못 한 경우 전경이 영영 멈춘다.
    return gatePlanRelayTimeoutMs(args);
  }
  return DEFAULT_RELAY_TIMEOUT_MS;
}

function safeSpawnSync(commandName, args, options = {}) {
  // spawnSync의 기본 SIGTERM은 자식이 무시하면 timeout 뒤에도 영원히 기다린다.
  // relay에는 무시할 수 없는 종료 신호를 써서 lifecycle 명령의 상한을 실제로 보장한다.
  const timeout = options.timeout ?? DEFAULT_RELAY_TIMEOUT_MS;
  const killSignal = options.killSignal ?? "SIGKILL";
  const result = spawnSync(commandName, args, {
    ...options,
    timeout,
    killSignal,
  });
  if (timeout && result.error?.code === "ETIMEDOUT") {
    const timeoutError = new Error(
      `${commandName} exceeded the ${timeout}ms agent-flow relay timeout`,
      { cause: result.error },
    );
    timeoutError.code = "ETIMEDOUT";
    result.error = timeoutError;
  }
  return result;
}

function readCurrentRun(root, worktree = null) {
  const stateRoot = pythonRunStateRoot(root, worktree);
  const runsRoot = path.join(stateRoot, ".agent-flow", "runs");
  if (!fs.existsSync(runsRoot)) {
    throw new Error('no active run. start one with: agent-flow run "<task>"');
  }
  const activeRuns = fs.readdirSync(runsRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && fs.existsSync(path.join(runsRoot, entry.name, "active")))
    .map((entry) => path.join(runsRoot, entry.name));
  if (activeRuns.length === 0) {
    throw new Error('no active run. start one with: agent-flow run "<task>"');
  }
  if (activeRuns.length > 1) {
    throw new Error(`multiple active Python runs found: ${activeRuns.join(", ")}`);
  }
  const runDir = activeRuns[0];
  const meta = readJsonIfExists(path.join(runDir, "meta.json"));
  if (!meta || typeof meta !== "object" || Array.isArray(meta)) {
    throw new Error(`active Python run has invalid meta.json: ${runDir}`);
  }
  return {
    ...meta,
    phase: meta.current_phase ?? null,
    run_dir: runDir,
  };
}

function pythonRunStateRoot(root, worktree = null) {
  const cwd = canonicalPath(process.cwd());
  const managed = resolveManagedWorktreeContext(cwd);
  // 채택 기록도 이름의 근거다. 새 기본 자리는 관리 경로 밖이므로 이걸 빼면 worktree
  // 안에서 leader의 run state를 읽어 phase가 어긋난다.
  const requestedName = worktree
    || (managed && samePath(managed.root, root) ? managed.name : null)
    || adoptedCheckoutName(root, cwd);
  if (!requestedName) {
    return root;
  }
  const worktreeName = resolveRegisteredWorktreeName(root, requestedName);
  const commonDir = gitOutput(root, ["rev-parse", "--git-common-dir"]);
  if (!commonDir) {
    throw new Error("cannot resolve Python worktree state without the git common dir");
  }
  return path.join(path.resolve(root, commonDir), "agent-flow", "worktrees", worktreeName);
}

function resolveRegisteredWorktreeName(root, selector) {
  const output = gitOutput(root, ["worktree", "list", "--porcelain", "-z"]);
  if (!output) {
    return selector;
  }
  const entries = [];
  let current = null;
  for (const field of output.split("\0").filter(Boolean)) {
    const separator = field.indexOf(" ");
    const key = separator === -1 ? field : field.slice(0, separator);
    const value = separator === -1 ? "" : field.slice(separator + 1);
    if (key === "worktree") {
      if (current) entries.push(current);
      current = { path: canonicalPath(value), branch: null, bare: false };
    } else if (current && key === "branch") {
      current.branch = value.startsWith("refs/heads/")
        ? value.slice("refs/heads/".length)
        : value;
    } else if (current && key === "bare") {
      current.bare = true;
    }
  }
  if (current) entries.push(current);

  const wanted = selector.trim();
  const pathCandidates = path.isAbsolute(wanted)
    ? [canonicalPath(wanted)]
    : [
      canonicalPath(path.resolve(root, wanted)),
      canonicalPath(path.resolve(process.cwd(), wanted)),
    ];
  const ranked = new Map();
  for (const entry of entries) {
    if (entry.bare || samePath(entry.path, root)) continue;
    let rank = null;
    if (pathCandidates.some((candidate) => samePath(candidate, entry.path))) {
      rank = 0;
    } else if (wanted === entry.branch) {
      rank = 1;
    } else if (wanted === path.basename(entry.path)) {
      rank = 2;
    } else {
      const derived = derivedWorktreeIdentity(wanted);
      if (
        derived
        && (
          derived.name === path.basename(entry.path)
          || derived.branch === entry.branch
        )
      ) {
        rank = 3;
      }
    }
    if (rank === null) continue;
    const bucket = ranked.get(rank) ?? new Map();
    bucket.set(entry.path, entry);
    ranked.set(rank, bucket);
  }
  if (ranked.size === 0) {
    return selector;
  }
  const bestRank = Math.min(...ranked.keys());
  const matches = [...ranked.get(bestRank).values()];
  if (matches.length !== 1) {
    throw new Error(
      `worktree selector is ambiguous: ${selector}; candidates: `
      + matches.map((entry) => `${entry.path} (${entry.branch ?? "detached"})`).join(", "),
    );
  }
  return path.basename(matches[0].path);
}

function derivedWorktreeIdentity(selector) {
  const lowered = selector.trim().toLowerCase();
  let safe = lowered.replace(/[^a-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "");
  if (!safe || safe.startsWith(".") || safe.includes("..")) {
    if (!/[\p{L}\p{N}]/u.test(lowered)) return null;
    const digest = crypto.createHash("sha1").update(lowered, "utf8").digest("hex").slice(0, 8);
    safe = `task-${digest}`;
  }
  const name = safe.startsWith("feat-") ? safe : `feat-${safe}`;
  return { name, branch: `feat/${name.slice("feat-".length)}` };
}

function resolveRunDir(root, runDir) {
  return path.isAbsolute(runDir) ? runDir : path.join(root, runDir);
}

function assertPythonRuntimeInstalled(root) {
  const installMarker = path.join(root, ".agent-flow", "kit.json");
  if (!fs.existsSync(installMarker)) {
    throw new Error("agent-flow is not installed. run: agent-flow-kit install");
  }
  const required = [
    installMarker,
    path.join(root, RUNTIME_PYTHON_RELATIVE, "agent_flow", "__init__.py"),
    path.join(root, RUNTIME_PYTHON_RELATIVE, "agent_flow", "cli.py"),
    path.join(
      root,
      RUNTIME_PYTHON_RELATIVE,
      "agent_flow",
      "templates",
      "_shared",
      "review",
      "architecture.md",
    ),
  ];
  const missing = required.filter((filePath) => !fs.existsSync(filePath));
  if (missing.length > 0) {
    throw new Error(
      `agent-flow is not installed or its Python runtime is incomplete. Re-run agent-flow-kit install. Missing: ${missing.join(", ")}`,
    );
  }
}

function assertInstalled(root) {
  const phases = fullFeaturePhases();
  const skillIndex = readJsonIfExists(path.join(root, ".agent-flow", "skills", "index.json"));
  const selectedSkillPaths = Array.isArray(skillIndex?.skills)
    ? skillIndex.skills
        .map((skill) => selectedSkillPath(root, skill))
        .filter(Boolean)
    : [];
  const required = [
    path.join(root, ".agent-flow", "kit.json"),
    path.join(root, ".agent-flow", "workflows", "full-feature.yaml"),
    path.join(root, ".agent-flow", "skills", "index.json"),
    path.join(root, ".agent-flow", "skills", "full-feature-workflow", "SKILL.md"),
    ...phases.map((phase) => path.join(root, ".agent-flow", "prompts", `${phase.id}.md`)),
    path.join(root, ".agent-flow", "skills", "domain-modeling", "SKILL.md"),
    path.join(root, ".agent-flow", "skills", "product-brief", "SKILL.md"),
    path.join(root, ".agent-flow", "skills", "plan-reviewer", "SKILL.md"),
    path.join(root, ".agent-flow", "skills", "architecture-reviewer", "SKILL.md"),
    path.join(root, ".agent-flow", "skills", "push-watch", "SKILL.md"),
    ...selectedSkillPaths,
    path.join(root, ".agent-flow", "prompts", "push-watch.md"),
    path.join(root, ".agent-flow", "prompts", "push-watch-tick.md"),
    path.join(root, ".agent-flow", "bootstrap", "AGENTS.md"),
    path.join(root, ".agent-flow", "bootstrap", "CLAUDE.md"),
    path.join(root, ".Codex", "agents", "code-reviewer.md"),
    path.join(root, ".claude", "agents", "code-reviewer.md"),
  ];
  const missing = required.filter((pathName) => !fs.existsSync(pathName));
  if (missing.length > 0) {
    throw new Error(`agent-flow is not installed. run: agent-flow-kit install`);
  }
  for (const codeReviewer of [
    path.join(root, ".Codex", "agents", "code-reviewer.md"),
    path.join(root, ".claude", "agents", "code-reviewer.md"),
  ]) {
    if (!fs.readFileSync(codeReviewer, "utf8").trim()) {
      throw new Error(`agent-flow is not installed correctly: ${path.relative(root, codeReviewer)} is empty`);
    }
  }
}

// install이 target으로 복사하는 자산의 지문. `.Codex/agents`와 `.claude/agents`는
// `assertInstalled`가 필수로 요구하는 파일이라 반드시 포함한다. `bin`/`lib`는
// 설치 산출물의 본문(`rules/workflow-contract.md`, 생성되는 SKILL.md 등)을 만들어
// 내는 소스라 여기 빠지면 그 변경이 지문에 안 잡힌다. host 경로는 installer가 kit에서
// **읽는** 자리만 넣는다 — `.Codex/hooks.json`, `.claude/settings.json`,
// `.Codex/rules/concise-output.md`는 installer가 target에 만들어 내는 산출물이라
// 넣으면 self-install 직후부터 자산 변경 없이 지문이 흔들린다.
// Python `core/kit_digest.py`와 같은 목록이어야 한다. parity가 두 값을 대조한다.
const KIT_SOURCE_DIGEST_ROOTS = [
  "templates",
  "bootstrap",
  "skills",
  "scripts",
  "src/agent_flow",
  "bin",
  "lib",
  ".Codex/agents",
  ".Codex/rules/context",
  ".Codex/rules/codebase-rubric.md",
  ".Codex/context",
  ".claude/agents",
];

// 파생 산출물은 소스가 아니다. `.pyc`는 헤더에 소스 mtime을 담는데 git은 mtime을
// 보존하지 않으므로, 넣으면 같은 커밋의 두 체크아웃이 다른 지문을 낸다.
// `scripts/check-agent-flow-parity.mjs`의 self-install 필터와 같은 목록이다.
const DIGEST_EXCLUDED_DIRS = new Set([
  ".agent-flow",
  ".git",
  ".mypy_cache",
  ".pytest_cache",
  ".ruff_cache",
  ".venv",
  "__pycache__",
  "node_modules",
]);

function kitSourceDigest() {
  const hash = crypto.createHash("sha256");
  for (const relativeRoot of KIT_SOURCE_DIGEST_ROOTS) {
    for (const filePath of walkFilesSorted(path.join(KIT_ROOT, relativeRoot))) {
      hash.update(path.relative(KIT_ROOT, filePath).split(path.sep).join("/"));
      hash.update("\0");
      hash.update(crypto.createHash("sha256").update(fs.readFileSync(filePath)).digest("hex"));
      hash.update("\0");
    }
  }
  return hash.digest("hex");
}


function walkFilesSorted(target) {
  if (!fs.existsSync(target)) {
    return [];
  }
  // digest root는 디렉터리일 수도 단일 파일일 수도 있다.
  if (!fs.statSync(target).isDirectory()) {
    return [target];
  }
  const dir = target;
  const files = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true }).sort((a, b) => (a.name < b.name ? -1 : 1))) {
    if (DIGEST_EXCLUDED_DIRS.has(entry.name) || entry.name.endsWith(".pyc")) {
      continue;
    }
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...walkFilesSorted(full));
    } else if (entry.isFile()) {
      files.push(full);
    }
  }
  return files;
}

// 낡은 설치본은 조용히 옛 workflow/profile/runtime을 돌린다. `skills sync`는
// 이것을 고치지 않는다 — 그 명령은 외부 skill_sources만 fetch한다.
function warnIfInstalledKitIsStale(root) {
  const payload = readJsonIfExists(path.join(root, ".agent-flow", "kit.json"));
  const recorded = payload?.kit_source_digest;
  if (typeof recorded !== "string" || !recorded) {
    // 이 지문을 기록하기 전에 설치된 프로젝트. 대조 기준이 없으므로 판정하지 않는다.
    return;
  }
  if (recorded === kitSourceDigest()) {
    return;
  }
  console.warn(
    "warning: the installed agent-flow assets are older than this kit "
    + "(workflows, profiles, skills, or the Python runtime changed). "
    + "run: agent-flow-kit install",
  );
}

function selectedSkillPath(root, skill) {
  if (!skill || typeof skill !== "object") {
    return null;
  }
  if (typeof skill.path === "string" && skill.path) {
    return path.isAbsolute(skill.path) ? skill.path : path.join(root, skill.path);
  }
  if (typeof skill.name === "string" && skill.name) {
    return path.join(root, ".agent-flow", "skills", skill.name, "SKILL.md");
  }
  return null;
}


function pushWatchStatePath(root) {
  return path.join(root, ".agent-flow", "state", "push-watch.json");
}


function pushWatchIntentPath(root) {
  return path.join(root, ".agent-flow", "state", "push-watch-intent.json");
}


function pushWatchLockPath(root) {
  return path.join(root, ".agent-flow", "state", "push-watch.lock");
}


function withPushWatchLock(root, operation) {
  const ownership = acquirePushWatchLock(root);
  try {
    return operation();
  } finally {
    releasePushWatchLock(ownership);
  }
}


function acquirePushWatchLock(root) {
  const lock = pushWatchLockPath(root);
  fs.mkdirSync(path.dirname(lock), { recursive: true });
  const token = randomBytes(16).toString("hex");
  const pending = `${lock}.${process.pid}.${token}.pending`;
  fs.mkdirSync(pending, { mode: 0o700 });
  atomicWriteFileSync(
    path.join(pending, "owner.json"),
    `${JSON.stringify({ pid: process.pid, token }, null, 2)}\n`,
    { mode: 0o600, durable: true },
  );
  try {
    fs.renameSync(pending, lock);
    fsyncParentDirectory(lock);
    return { lock, token };
  } catch (error) {
    fs.rmSync(pending, { recursive: true, force: true });
    if (error?.code !== "EEXIST" && error?.code !== "ENOTEMPTY") {
      throw error;
    }
    if (reclaimStalePushWatchLock(lock)) {
      return acquirePushWatchLock(root);
    }
    throw new Error("blocked: push-watch tick is already active");
  }
}


function reclaimStalePushWatchLock(lock) {
  let owner;
  try {
    owner = JSON.parse(fs.readFileSync(path.join(lock, "owner.json"), "utf8"));
  } catch {
    return false;
  }
  if (
    !Number.isInteger(owner?.pid)
    || typeof owner?.token !== "string"
    || !/^[0-9a-f]{32}$/.test(owner.token)
  ) {
    return false;
  }
  if (processIsAlive(owner.pid)) {
    return false;
  }
  const retired = `${lock}.retired-${owner.token}`;
  try {
    fs.renameSync(lock, retired);
  } catch (error) {
    if (
      error?.code === "ENOENT"
      || error?.code === "EEXIST"
      || error?.code === "ENOTEMPTY"
    ) {
      return false;
    }
    throw error;
  }
  fsyncParentDirectory(lock);
  return true;
}


function processIsAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    if (error?.code === "ESRCH") {
      return false;
    }
    if (error?.code === "EPERM") {
      return true;
    }
    throw error;
  }
}


function releasePushWatchLock({ lock, token }) {
  const owner = JSON.parse(fs.readFileSync(path.join(lock, "owner.json"), "utf8"));
  if (owner?.token !== token) {
    throw new Error("blocked: push-watch lock ownership changed");
  }
  fs.rmSync(lock, { recursive: true });
  fsyncParentDirectory(lock);
}



function pushWatchArtifact(status, prUrl, recordedAt) {
  return [
    `status: ${status}`,
    `pr: ${prUrl ?? "unknown"}`,
    `recorded_at: ${recordedAt}`,
    "",
  ].join("\n");
}


function commitPushWatchObservation(root, runDir, runId, pr, watchStatus) {
  if (typeof runId !== "string" || !runId) {
    throw new Error("blocked: active push-watch run has no run id");
  }
  const observationId = crypto
    .createHash("sha256")
    .update(JSON.stringify({ runId, pr, watchStatus }))
    .digest("hex");
  while (true) {
    replayPushWatchIntent(root, runDir, runId);
    const previous = fs.existsSync(pushWatchStatePath(root))
      ? JSON.parse(fs.readFileSync(pushWatchStatePath(root), "utf8"))
      : {};
    const prUrl = pr.url ?? null;
    if (previous.observation_id === observationId) {
      writeManagedFile(
        path.join(runDir, "artifacts", "pr-watch.md"),
        pushWatchArtifact(watchStatus, prUrl, previous.updated_at),
        { durable: true },
      );
      return previous;
    }
    const recordedAt = new Date().toISOString();
    const artifact = pushWatchArtifact(watchStatus, prUrl, recordedAt);
    const state = {
      ...previous,
      run_id: runId,
      run_dir: runDir,
      status: watchStatus,
      pr: prUrl,
      observation_id: observationId,
      iterations: Number(previous.iterations ?? 0) + 1,
      updated_at: recordedAt,
    };
    if (!publishPushWatchIntent(root, {
      version: 1,
      run_id: runId,
      run_dir: runDir,
      observation_id: observationId,
      artifact,
      state,
    })) {
      continue;
    }
    replayPushWatchIntent(root, runDir, runId);
    return state;
  }
}


function publishPushWatchIntent(root, payload) {
  const intent = pushWatchIntentPath(root);
  fs.mkdirSync(path.dirname(intent), { recursive: true });
  const pending = `${intent}.${process.pid}.${randomBytes(8).toString("hex")}.pending`;
  atomicWriteFileSync(pending, `${JSON.stringify(payload, null, 2)}\n`, {
    mode: 0o600,
    durable: true,
  });
  try {
    fs.linkSync(pending, intent);
    fsyncParentDirectory(intent);
    return true;
  } catch (error) {
    if (error?.code === "EEXIST") {
      return false;
    }
    throw error;
  } finally {
    fs.rmSync(pending, { force: true });
  }
}


function replayPushWatchIntent(root, runDir, runId) {
  const intent = pushWatchIntentPath(root);
  if (!fs.existsSync(intent)) {
    return null;
  }
  const payload = JSON.parse(fs.readFileSync(intent, "utf8"));
  if (
    payload?.version !== 1
    || typeof payload.run_id !== "string"
    || typeof payload.run_dir !== "string"
    || !path.isAbsolute(payload.run_dir)
    || path.basename(payload.run_dir) !== payload.run_id
    || typeof payload.observation_id !== "string"
    || typeof payload.artifact !== "string"
    || !payload.state
    || payload.state.run_id !== payload.run_id
    || payload.state.run_dir !== payload.run_dir
    || payload.state.observation_id !== payload.observation_id
  ) {
    throw new Error("blocked: push-watch intent is malformed");
  }
  if (payload.run_id !== runId || !samePath(payload.run_dir, runDir)) {
    if (fs.existsSync(path.join(payload.run_dir, "active"))) {
      throw new Error(
        `blocked: push-watch intent belongs to another active run (${payload.run_dir}); `
        + "finish or abort that run before retrying push-watch",
      );
    }
    fs.rmSync(intent);
    fsyncParentDirectory(intent);
    return null;
  }
  writeManagedFile(
    path.join(runDir, "artifacts", "pr-watch.md"),
    payload.artifact,
    { durable: true },
  );
  writeJson(pushWatchStatePath(root), payload.state, { durable: true });
  fs.rmSync(intent, { force: true });
  fsyncParentDirectory(intent);
  return payload.state;
}


function fsyncParentDirectory(pathName) {
  let descriptor = null;
  try {
    descriptor = fs.openSync(path.dirname(pathName), "r");
    fs.fsyncSync(descriptor);
  } catch {
    // Directory fsync is not portable; file publication is still atomic.
  } finally {
    if (descriptor !== null) {
      fs.closeSync(descriptor);
    }
  }
}

function currentBranch(root) {
  const result = safeSpawnSync("git", ["branch", "--show-current"], {
    cwd: root,
    env: gitEnv(),
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
  const branch = result.stdout?.trim() ?? "";
  if (result.error || result.status !== 0 || !branch) {
    throw new Error("blocked: push-watch requires a named git branch");
  }
  return branch;
}

function readPullRequestStatus(root) {
  const result = safeSpawnSync("gh", ["pr", "view", "--json", "url,reviewDecision,statusCheckRollup"], {
    cwd: root,
    encoding: "utf8",
  });
  if (result.status !== 0) {
    const detail = result.error?.message ?? result.stderr?.trim() ?? "unknown error";
    throw new Error(`blocked: gh pr view failed: ${detail}`);
  }
  return JSON.parse(result.stdout);
}

function pullRequestWatchStatus(pr) {
  const reviewDecision = String(pr.reviewDecision ?? "").toUpperCase();
  const checks = Array.isArray(pr.statusCheckRollup) ? pr.statusCheckRollup : [];
  const hasFailedCheck = checks.some((check) =>
    ["FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"].includes(
      String(check.conclusion ?? check.state ?? "").toUpperCase(),
    ),
  );
  if (hasFailedCheck) {
    return "ci-failed";
  }
  if (reviewDecision === "CHANGES_REQUESTED") {
    return "comments";
  }
  const hasPendingCheck =
    checks.length === 0 ||
    checks.some((check) => {
      const status = String(check.status ?? "").toUpperCase();
      const conclusion = String(check.conclusion ?? "").toUpperCase();
      const state = String(check.state ?? "").toUpperCase();
      if (state) {
        return state !== "SUCCESS";
      }
      return status !== "COMPLETED" || (conclusion !== "SUCCESS" && conclusion !== "SKIPPED");
    });
  if (hasPendingCheck || reviewDecision !== "APPROVED") {
    return "pending";
  }
  return "green";
}


function optionValue(args, name) {
  const index = args.indexOf(name);
  if (index === -1) {
    return undefined;
  }
  return args[index + 1];
}

function writeJson(pathName, payload, options = {}) {
  atomicWriteFileSync(pathName, `${JSON.stringify(payload, null, 2)}\n`, options);
}

function readExistingKit(agentFlowDir) {
  const kitPath = path.join(agentFlowDir, "kit.json");
  if (!fs.existsSync(kitPath)) {
    return undefined;
  }
  try {
    return JSON.parse(fs.readFileSync(kitPath, "utf8"));
  } catch {
    return undefined;
  }
}

function writeFileIfMissing(pathName, content) {
  fs.mkdirSync(path.dirname(pathName), { recursive: true });
  if (!fs.existsSync(pathName)) {
    fs.writeFileSync(pathName, content, "utf8");
  }
}

function writeManagedFile(pathName, content, options = {}) {
  atomicWriteFileSync(pathName, content, options);
}

function writeManagedFileIfMissingOrSame(pathName, content, force = false) {
  fs.mkdirSync(path.dirname(pathName), { recursive: true });
  if (fs.existsSync(pathName)) {
    const current = fs.readFileSync(pathName, "utf8");
    if (force) {
      atomicWriteFileSync(pathName, content);
      return true;
    }
    if (current !== content) {
      return false;
    }
    return true;
  }
  atomicWriteFileSync(pathName, content);
  return true;
}

function copyBundledDirIfMissingOrSame(
  src,
  dest,
  force = false,
  excludedRootDirs = new Set(),
  isRoot = true,
  pruneExtraneous = false,
  preservedExtraneousRootNames = new Set(),
  allowedRootDirs = null,
  backupPrunedRoot = null,
) {
  if (!fs.existsSync(src)) {
    return;
  }
  fs.mkdirSync(dest, { recursive: true });
  const sourceNames = new Set();
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    sourceNames.add(entry.name);
    const srcPath = path.join(src, entry.name);
    const destPath = path.join(dest, entry.name);
    // 대상 자리가 링크면 여기서 끝난다. 제거 분기로 보내면 링크(혹은 링크 너머의
    // 실제 디렉터리)를 지우고, 복사 분기로 보내면 링크 너머 - 대개 프로젝트 밖 -
    // 에 번들 파일을 만든다. 둘 다 우리가 소유를 증명하지 못한 자리다.
    if (isSymlinkPath(destPath)) {
      console.log(`${SYMLINK_SKIP_NOTICE_PREFIX}${destPath}`);
      continue;
    }
    if (entry.isDirectory()) {
      if (isRoot && allowedRootDirs && !allowedRootDirs.has(entry.name)) {
        removeManagedDirIfSame(srcPath, destPath, force);
        continue;
      }
      if (isRoot && excludedRootDirs.has(entry.name)) {
        removeManagedDirIfSame(srcPath, destPath, force);
        continue;
      }
      copyBundledDirIfMissingOrSame(srcPath, destPath, force, excludedRootDirs, false, pruneExtraneous, preservedExtraneousRootNames, null, backupPrunedRoot);
      continue;
    }
    if (!entry.isFile()) {
      continue;
    }
    const content = fs.readFileSync(srcPath, "utf8");
    writeManagedFileIfMissingOrSame(destPath, content, force);
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
}


// prune은 source에 없는 파일을 지운다. 사용자가 직접 만든 workflow가 여기
// 걸리면 경고도 사본도 없이 사라졌다. 같은 내용이 이미 백업돼 있으면 다시
// 쓰지 않는다 — 재설치마다 사본이 불어나면 그것대로 잃는 것과 같다.

function removeManagedDirIfSame(src, dest, force = false) {
  if (!fs.existsSync(dest)) {
    return;
  }
  // 대상이 git이 추적하는 소스면 건드리지 않는다. worktree의 kit으로 leader를
  // install하면 `root !== KIT_ROOT`라 이 경로가 돌고, leader의 추적 중인
  // `scripts/`가 통째로 삭제됐다(실측: `git status`에 `D scripts/*` 7개).
  // 관리 사본을 걷어내는 것이 목적이지 사용자 소스를 지우는 것이 아니다.
  if (isGitTracked(dest)) {
    return;
  }
  if (!force && !dirContentsMatch(src, dest)) {
    return;
  }
  fs.rmSync(dest, { recursive: true, force: true });
}

function isGitTracked(target) {
  const result = spawnSync("git", ["ls-files", "--error-unmatch", "-z", "--", "."], {
    cwd: fs.statSync(target).isDirectory() ? target : path.dirname(target),
    env: gitEnv(),
    encoding: "utf8",
    timeout: 10_000,
  });
  return !result.error && result.status === 0 && result.stdout.trim().length > 0;
}

function dirContentsMatch(src, dest) {
  if (!fs.existsSync(src) || !fs.existsSync(dest)) {
    return false;
  }
  const srcEntries = fs.readdirSync(src, { withFileTypes: true });
  const destEntries = fs.readdirSync(dest, { withFileTypes: true });
  if (srcEntries.length !== destEntries.length) {
    return false;
  }
  const destByName = new Map(destEntries.map((entry) => [entry.name, entry]));
  for (const srcEntry of srcEntries) {
    const destEntry = destByName.get(srcEntry.name);
    if (!destEntry || srcEntry.isDirectory() !== destEntry.isDirectory() || srcEntry.isFile() !== destEntry.isFile()) {
      return false;
    }
    const srcPath = path.join(src, srcEntry.name);
    const destPath = path.join(dest, srcEntry.name);
    if (srcEntry.isDirectory()) {
      if (!dirContentsMatch(srcPath, destPath)) {
        return false;
      }
      continue;
    }
    if (srcEntry.isFile() && fs.readFileSync(srcPath, "utf8") !== fs.readFileSync(destPath, "utf8")) {
      return false;
    }
  }
  return true;
}

function installProjectSkills(root, agentFlowDir, previousIndex, force = false, installSelection = null) {
  const selected = selectProjectSkills(root, agentFlowDir, installSelection);
  const links = [];
  for (const skill of selected.skills) {
    // bundled skill 중 host 디렉토리 link 대상은 BUNDLED_HOST_SKILL_NAMES뿐이다.
    // 나머지 bundled skill은 index에만 노출해 agent가 발견할 수 있게 한다.
    if (skill.source === "bundled" && !BUNDLED_HOST_SKILL_NAMES.has(skill.name)) {
      continue;
    }
    for (const host of skill.hosts) {
      links.push(linkProjectSkill(root, skill, host, previousIndex, force));
    }
  }
  links.push(...removeStaleProjectSkillLinks(root, selected.skills, previousIndex, force));
  const index = preserveKitSkillHashes({ ...selected, links }, previousIndex, path.join(KIT_ROOT, "skills"));
  // 잘린 index.json은 `readJsonIfExists`가 조용히 삼켜 "설치된 skill 없음"으로
  // 읽힌다. 그러면 다음 install이 관리 링크를 stale로 보고 걷어낸다.
  atomicWriteFileSync(
    path.join(agentFlowDir, "skills", "index.json"),
    `${JSON.stringify(index, null, 2)}\n`,
  );
  return index;
}

function selectProjectSkills(root, agentFlowDir, installSelection = null) {
  const discovered = [
    ...discoverSkills(path.join(agentFlowDir, "local-skills"), "local", root, PROFILE_MANAGED_HOST_ONLY_SKILLS),
    ...discoverProjectSkills(root),
    ...discoverSkills(
      path.join(agentFlowDir, "skills"),
      "bundled",
      root,
      new Set(["index.json", "catalog.lock.json", ...PROFILE_MANAGED_HOST_ONLY_SKILLS]),
    ),
  ];
  const byName = new Map();
  const warnings = [];
  for (const skill of discovered) {
    const current = byName.get(skill.name);
    if (!current || skill.priority < current.priority) {
      byName.set(skill.name, skill);
    }
    warnings.push(...skill.warnings);
  }
  const allowed = installSelection?.skillNames || null;
  const skills = [...byName.values()]
    .filter((skill) => skill.source !== "bundled" || !allowed || allowed.has(skill.name))
    .sort((a, b) => a.name.localeCompare(b.name));
  warnings.push(...validateSkillDependencies(skills));
  const conflicts = [];
  for (const skill of skills) {
    const ignored = discovered
      .filter((candidate) => candidate.name === skill.name && candidate.path !== skill.path)
      .sort((a, b) => a.priority - b.priority)
      .map((candidate) => candidate.path);
    if (ignored.length > 0) {
      conflicts.push({ name: skill.name, selected: skill.path, ignored });
    }
  }
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



function discoverProjectSkills(root) {
  if (samePath(root, KIT_ROOT)) {
    return [];
  }
  return discoverSkills(path.join(root, "skills"), "project", root, PROFILE_MANAGED_HOST_ONLY_SKILLS);
}

function discoverSkills(baseDir, source, root, ignoredNames = new Set(), allowedNames = null) {
  if (!fs.existsSync(baseDir)) {
    return [];
  }
  const priority = { local: 0, project: 1, bundled: 2 }[source] ?? 99;
  const skills = [];
  for (const entry of fs.readdirSync(baseDir, { withFileTypes: true })) {
    if (!entry.isDirectory() || ignoredNames.has(entry.name)) {
      continue;
    }
    if (allowedNames && !allowedNames.has(entry.name)) {
      continue;
    }
    const skillPath = path.join(baseDir, entry.name, "SKILL.md");
    if (!fs.existsSync(skillPath)) {
      continue;
    }
    const text = fs.readFileSync(skillPath, "utf8");
    const metadata = parseSkillMetadata(text, entry.name, PROJECT_SKILL_HOSTS, source);
    if (ignoredNames.has(metadata.name)) {
      continue;
    }
    const relativePath = path.relative(root, skillPath);
    const contentObservation = observeSkillContent(path.dirname(skillPath));
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
      governance: metadata.governance,
      observedContentDigest: contentObservation.digest,
      hash: crypto.createHash("sha256").update(text).digest("hex"),
      priority,
      warnings: [
        ...metadata.warnings.map((message) => `${relativePath}: ${message}`),
        ...(contentObservation.warning
          ? [`${relativePath}: ${contentObservation.warning}`]
          : []),
      ],
    });
  }
  return skills;
}








function removeStaleProjectSkillLinks(root, skills, previousIndex, force = false) {
  if (!previousIndex || !Array.isArray(previousIndex.links)) {
    return [];
  }
  const desired = new Set(skills.flatMap((skill) => skill.hosts.map((host) => `${host}:${skill.name}`)));
  const removed = [];
  for (const link of previousIndex.links) {
    if (!link || !link.name || !link.host || !link.path) {
      continue;
    }
    const key = `${link.host}:${link.name}`;
    if (desired.has(key)) {
      continue;
    }
    const target = path.join(root, link.path);
    // 과거 index는 .codex(소문자) 경로를 기록했다. case-sensitive FS에서
    // ensureChildPath가 .Codex와 어긋나 throw하지 않도록 기록된 casing을 따른다.
    const hostRoot = legacyHostSkillRoot(root, link.path) ?? hostSkillRoot(root, link.host);
    if (pathHasSymlink(root, hostRoot)) {
      removed.push({ name: link.name, host: link.host, path: link.path, status: "skipped-host-root-symlink" });
      continue;
    }
    ensureChildPath(hostRoot, target);
    const stat = lstatIfExists(target);
    if (!stat) {
      continue;
    }
    if (stat.isSymbolicLink()) {
      fs.unlinkSync(target);
      removed.push({ name: link.name, host: link.host, path: link.path, status: "removed-stale" });
      continue;
    }
    if (force) {
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
    if (error && error.code === "ENOENT") {
      return null;
    }
    throw error;
  }
}

function linkProjectSkill(root, skill, host, previousIndex, force = false) {
  const srcDir = path.dirname(path.join(root, skill.path));
  const hostRoot = hostSkillRoot(root, host);
  if (pathHasSymlink(root, hostRoot)) {
    return { name: skill.name, host, path: path.relative(root, hostRoot), status: "skipped-host-root-symlink" };
  }
  const destDir = path.join(hostRoot, skill.name);
  ensureChildPath(hostRoot, destDir);
  const destSkill = path.join(destDir, "SKILL.md");
  const previousHash = previousSkillHash(previousIndex, skill.name);
  // `existsSync`는 심링크를 **따라가서** 끊어진 링크에 false를 준다. 그러면 stale
  // link 정리 분기를 못 타고 곧바로 링크 생성이 EEXIST로 죽는다. profile을 좁히거나
  // `--skills` 선택을 바꾸면 이전 선택의 host 링크가 끊긴 채 남으므로 실제로 밟는다.
  // lstat은 링크 자체를 보므로 끊어진 링크도 정리 대상으로 잡힌다.
  const destStat = lstatIfExists(destDir);
  if (destStat) {
    if (destStat.isSymbolicLink()) {
      fs.unlinkSync(destDir);
    } else if (fs.existsSync(destSkill)) {
      const currentHash = crypto.createHash("sha256").update(fs.readFileSync(destSkill, "utf8")).digest("hex");
      if (!force && currentHash !== skill.hash && currentHash !== previousHash) {
        return { name: skill.name, host, path: path.relative(root, destDir), status: "skipped-user-modified" };
      }
      fs.rmSync(destDir, { recursive: true, force: true });
    } else if (force) {
      fs.rmSync(destDir, { recursive: true, force: true });
    } else {
      return { name: skill.name, host, path: path.relative(root, destDir), status: "skipped-existing" };
    }
  }
  fs.mkdirSync(path.dirname(destDir), { recursive: true });
  const relTarget = path.relative(path.dirname(destDir), srcDir);
  try {
    fs.symlinkSync(relTarget, destDir, "dir");
    return { name: skill.name, host, path: path.relative(root, destDir), status: "linked" };
  } catch {
    copyBundledDirIfMissingOrSame(srcDir, destDir, true);
    return { name: skill.name, host, path: path.relative(root, destDir), status: "copied" };
  }
}

function hostSkillRoot(root, host) {
  // case-sensitive FS에서 .codex/.Codex가 갈라지지 않도록 .Codex로 고정한다.
  if (host === "codex") {
    return path.join(root, ".Codex", "skills");
  }
  if (host === "omp") {
    return path.join(root, ".omp", "skills");
  }
  return path.join(root, `.${host}`, "skills");
}

function legacyHostSkillRoot(root, linkPath) {
  const normalized = String(linkPath).replaceAll("\\", "/");
  if (normalized.startsWith(".codex/skills/")) {
    return path.join(root, ".codex", "skills");
  }
  // gemini/antigravity host는 제거됐지만 과거 index가 기록한 link 정리는
  // 계속돼야 한다. hostSkillRoot로 유도하면 .antigravity/skills처럼 실제
  // 경로와 어긋나 ensureChildPath가 throw하며 install이 중단된다.
  if (normalized.startsWith(".gemini/antigravity/skills/")) {
    return path.join(root, ".gemini", "antigravity", "skills");
  }
  if (normalized.startsWith(".gemini/skills/")) {
    return path.join(root, ".gemini", "skills");
  }
  return null;
}

function previousSkillHash(previousIndex, name) {
  if (!previousIndex || !Array.isArray(previousIndex.skills)) {
    return "";
  }
  const previous = previousIndex.skills.find((skill) => skill && skill.name === name);
  return previous?.hash || "";
}





function preferredPython() {
  const virtualEnvPython = process.env.VIRTUAL_ENV
    ? path.join(process.env.VIRTUAL_ENV, process.platform === "win32" ? "Scripts/python.exe" : "bin/python")
    : null;
  // HOME이 바뀌면 user-site의 yaml을 잃는 시스템 python 대신 kit 자체 venv를 우선한다.
  const kitVenvPython = path.join(KIT_ROOT, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  const leaderRoot = resolveManagedWorktreeRoot(KIT_ROOT);
  const leaderVenvPython = leaderRoot
    ? path.join(leaderRoot, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python")
    : null;
  const candidates = [
    process.env.PYTHON,
    process.env.PYTHON_EXECUTABLE,
    virtualEnvPython,
    fs.existsSync(kitVenvPython) ? kitVenvPython : null,
    leaderVenvPython && fs.existsSync(leaderVenvPython) ? leaderVenvPython : null,
    "python3.12",
    "python3.11",
    "python3.10",
    "python3",
    "python",
  ].filter(Boolean);
  for (const candidate of candidates) {
    const result = safeSpawnSync(candidate, ["--version"], { stdio: "ignore" });
    if (!result.error && result.status === 0 && pythonSupportsWorkflowExport(candidate)) {
      return candidate;
    }
  }
  // 이 경로는 workflow export 말고도 spec confirm과 status가 함께 탄다. 무엇이
  // 없는지와 무엇을 하면 되는지를 같이 적지 않으면 사용자는 인터프리터를 손으로
  // 찾아 헤매다 PyYAML 없는 python을 타고 ModuleNotFoundError를 본다.
  throw new Error(
    "no Python with PyYAML found. Install it (pip install pyyaml) or point PYTHON at an interpreter that has it. "
    + `Tried: ${candidates.join(", ")}`,
  );
}

function pythonSupportsWorkflowExport(candidate) {
  const result = safeSpawnSync(candidate, ["-c", "import yaml"], {
    stdio: "ignore",
    timeout: 5_000,
  });
  return !result.error && result.status === 0;
}

function assertFreshArtifact(state, phase, artifact) {
  if (!artifactIsStale(state, artifact)) {
    return;
  }
  throw new Error(`blocked: stale artifact ${artifact}`);
}

function artifactIsStale(state, artifact) {
  const enteredAt = Date.parse(state.phase_entered_at ?? state.updated_at ?? state.started_at ?? "");
  if (!Number.isFinite(enteredAt)) {
    return false;
  }
  const artifactMtime = fs.statSync(artifact).mtimeMs;
  return artifactMtime < enteredAt;
}

// local-skill 판정은 Python resolver 한 곳에만 있다. JS는 workflow export와 같은 방식으로 위임한다.
// 여기에 휴리스틱을 다시 구현하면 Python과 조용히 갈라진다 — 그게 이 저장소의 최대 유지보수 리스크였다.
function runSkillsCli(args) {
  const result = safeSpawnSync(preferredPython(), ["-m", "agent_flow.cli", "skills", ...args], {
    cwd: KIT_ROOT,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    env: {
      ...process.env,
      PYTHONPATH: [path.join(KIT_ROOT, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
    },
    timeout: 15_000,
  });
  if (result.error || result.status !== 0) {
    const detail = result.error?.message || result.stderr?.trim() || `exit ${result.status}`;
    process.stderr.write(`agent-flow: skills ${args[0]} failed: ${detail}\n`);
    return null;
  }
  return result.stdout;
}

function readGatesPassed(pathName, nonce = "") {
  return readGatesRouteKey(pathName, nonce) === "green";
}

// run meta의 nonce와 대조한다. 이 층이 막는 것은 손으로 쓴 gate-results.json이지
// 적대적 위조가 아니다 — nonce도 디스크에 있으므로 읽어서 복사할 수 있다.
function gateNonceMatches(data, nonce) {
  if (!nonce) {
    return true;
  }
  return Boolean(data.produced_by) && data.produced_by.nonce === nonce;
}

// `agent-flow gates`의 기본 phase는 `pre-commit`이고 build/test는 `pre-push`다.
// Python `_gate_phase_covers_verification`과 같은 규칙이다.
const GATE_PHASE_ALL = "all";

// Python `runner.GATE_MALFORMED`와 같은 값. 읽을 수 없는 결과는 실패가 아니라
// 판정 불가이고, 어떤 workflow route도 이 key를 target으로 갖지 않는다.
const GATE_MALFORMED = "malformed-results";

function gatePhaseCoversVerification(data) {
  if (!data.produced_by || typeof data.produced_by !== "object") {
    return true;
  }
  const recorded = data.produced_by.gate_phase;
  if (typeof recorded !== "string" || !recorded) {
    return true;
  }
  return recorded === GATE_PHASE_ALL;
}

function readGatesRouteKey(pathName, nonce = "") {
  let content;
  try {
    content = fs.readFileSync(pathName, "utf8");
  } catch (err) {
    // 파일이 **없다는** 것만 "게이트를 아직 안 돌렸다"다. 내용이 깨진 것과 같은
    // 사유로 접으면 정상적인 "아직 안 함"이 차단으로 보고된다. 반대로 존재하는
    // 파일의 EACCES·EISDIR·ELOOP까지 여기서 삼키면 읽지도 못한 결과가 "안 돌림"이
    // 되어 fix-loop가 근거 없이 돈다 — Python 쌍둥이(`runner._next_index`)와 같이
    // 판정 불가로 보낸다.
    if (err?.code === "ENOENT") {
      return "default";
    }
    return GATE_MALFORMED;
  }
  let data;
  try {
    data = JSON.parse(content);
  } catch {
    return GATE_MALFORMED;
  }
  if (!data || typeof data !== "object" || Array.isArray(data) || typeof data.passed !== "boolean") {
    return GATE_MALFORMED;
  }
  try {
    if (!Array.isArray(data.results) || data.results.length === 0) {
      if (typeof data.status === "string") {
        const status = data.status.trim().toLowerCase().replace(/_/g, "-");
        if (data.passed === false && ["request-changes", "blocked", "error", "pending"].includes(status)) {
          return status;
        }
      }
      return data.passed === false ? "request-changes" : "default";
    }
    // timeout은 "실패"가 아니라 "판정 불가"다. Python runner와 같은 규칙이다.
    if (data.results.some((r) => r && r.timed_out === true)) {
      return "error";
    }
    // 완료 보고는 실제 실행한 gate command와 결과 evidence가 함께 있을 때만 허용한다.
    const requiredResults = data.results.filter((r) => r && r.required !== false);
    const resultsPass =
      gateNonceMatches(data, nonce) &&
      gatePhaseCoversVerification(data) &&
      requiredResults.length > 0 &&
      requiredResults.every((r) =>
        r &&
        typeof r.command === "string" &&
        r.command.trim().length > 0 &&
        hasGateEvidence(r) &&
        (r.passed === true || r.status === "pass" || r.status === "ok"),
      );
    if (typeof data.status === "string") {
      const status = data.status.trim().toLowerCase().replace(/_/g, "-");
      if (data.passed === true && ["green", "approve"].includes(status)) {
        return resultsPass ? status : "default";
      }
      if (data.passed === false && ["request-changes", "blocked", "error", "pending"].includes(status)) {
        return status;
      }
    }
    if (data.passed === true) {
      return resultsPass ? "green" : "default";
    }
    return "request-changes";
  } catch {
    return "default";
  }
}

function hasGateEvidence(result) {
  for (const key of ["output", "stdout", "stderr", "artifact", "path"]) {
    if (typeof result[key] === "string" && result[key].trim().length > 0) {
      return true;
    }
  }
  for (const key of ["exit_code", "exitCode"]) {
    if (Number.isInteger(result[key]) && result[key] === 0) {
      return true;
    }
  }
  return false;
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


// 루트 `AGENTS.md`/`CLAUDE.md` 블록과 `.agent-flow/bootstrap/<label>` 관리 사본은
// 둘 다 이 한 벌에서 나온다: `bootstrap/<label>.template`.
//
// 예전에는 두 생성기가 각자 텍스트를 들고 있었고, 그 벌들이 실제로 갈라졌다:
// 신규 install(`agent-flow-install.mjs`)은 템플릿을 쓰고 이 파일은 리터럴을 써서,
// 같은 프로젝트의 AGENTS.md가 어느 쪽이 마지막으로 돌았는지에 따라 다른 내용이 됐다.
// parity 검사는 그 리터럴을 아예 보지 않아 드리프트를 잡지도 못했다.
//
// 템플릿을 못 읽으면 던진다. 조용히 지나가면 "블록을 안 쓴 것"이 정상 결과가 되고,
// 리터럴 시절 내용이 남은 파일이 그대로 방치돼 드리프트가 되살아난다.
function readBootstrapTemplate() {
  const templatePath = path.join(KIT_ROOT, "bootstrap", BOOTSTRAP_TEMPLATE_FILE);
  try {
    return { path: templatePath, text: fs.readFileSync(templatePath, "utf8") };
  } catch (error) {
    throw new Error(`bootstrap template unreadable: ${templatePath} (${error?.message || error})`);
  }
}










// `.agent-flow/bootstrap/<label>`는 루트 블록의 **관리 사본**이다 — 루트 파일을 지웠거나
// 손으로 고친 프로젝트에서 "install이 심는 계약이 원래 무엇이었나"를 확인하는 자리다.
//
// 예전에는 이 함수가 자기 문구를 지어냈고, 그래서 정확히 예고된 대로 갈라졌다:
// 템플릿에만 있는 `--workflow` 선택 기준, leader 빌드 금지, gates-only 검증 정책 셋이
// 사본에는 없었다. parity의 needle 루프는 템플릿만 보므로 그 드리프트를 잡지도 못했다.
// 그래서 이제 짓지 않고 파생시킨다: 제목 한 줄 + 마커를 뗀 정본 본문.
//
// 본문은 `rootBootstrapBlock`이 그 label에 대해 내는 것과 같아야 한다. 사본이 루트에
// 실제로 심긴 것과 다르면 "원래 무엇이었나"를 확인하러 온 사람에게 거짓을 말한다 —
// `AGENTS.md`는 계약 본문, `CLAUDE.md`는 `@AGENTS.md` 포인터다.

// 마커는 루트 파일 **안에서** 블록의 경계를 표시하는 장치라 독립 사본에는 뜻이 없다.
// 리터럴 대신 패턴으로 잡는다: parity가 두 installer 소스에 마커 리터럴이 다시
// 나타나는 것을 금지한다 — 그 리터럴은 "템플릿에서 읽는다"가 "여기서 짓는다"로
// 되돌아갔다는 신호이기 때문이다.
const BOOTSTRAP_MARKER_LINE = /^<!--\s*agent-flow:([a-z:]*)\s*-->$/;

function managedBootstrapMarkdown(label) {
  const template = readBootstrapTemplate();
  const lines = rootBootstrapBlock(label, template.text).split(/\r?\n/);
  const body = [];
  let insideIndexBlock = false;
  for (const line of lines) {
    const marker = BOOTSTRAP_MARKER_LINE.exec(line.trim());
    if (marker) {
      // 인덱스 자리(skill·docs)는 install이 **루트 파일에만** 채운다. 사본에 그 자리를
      // 남기면 "아직 없다. install이 채운다"가 영원히 참이 되지 않는 문장으로 굳는다.
      // 인덱스별로 이 판정을 따로 적으면 새로 더한 인덱스만 사본으로 새어 나간다.
      if (marker[1].endsWith(":start")) insideIndexBlock = true;
      else if (marker[1].endsWith(":end")) insideIndexBlock = false;
      continue;
    }
    if (insideIndexBlock) continue;
    body.push(line);
  }
  const text = body.join("\n").trim();
  if (!text) {
    throw new Error(`bootstrap template has no body outside markers: ${template.path}`);
  }
  return `# ${label} Agent Flow Bootstrap\n\n${text}\n`;
}

function newRunId() {
  const stamp = new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
  return `${stamp}-${Math.random().toString(16).slice(2, 10)}`;
}

function fullFeatureWorkflowYaml() {
  return fullFeatureWorkflow().text;
}








function pushWatchPromptMarkdown() {
  return `# push-watch\n\nCommit, push, open a PR, and start the PR watch loop.\n\nUse \`${AGENT_FLOW_COMMAND} run push-watch\`.\n\nDo not run on protected branches. Do not merge without explicit approval.\n`;
}

function pushWatchTickPromptMarkdown() {
  return `# push-watch-tick\n\nPoll the current PR checks and review threads.\n\nUse \`${AGENT_FLOW_COMMAND} run push-watch-tick\`.\n\nWrite \`artifacts/pr-watch.md\` with one status line: \`status: green\`, \`status: comments\`, \`status: ci-failed\`, or \`status: pending\`.\n`;
}

function phasePrompt(phase) {
  const markers = phase.required_markers?.length
    ? `\n\n## Completion markers\n\nThe runner blocks this phase until the artifact includes a \`## Completion Gate\` section with these marker lines:\n\n${phase.required_markers.map((marker) => `- \`${marker}\``).join("\n")}\n`
    : "";
  // skill 목록은 여기에 굽지 않는다. install 시점 스냅샷은 새 skill이 설치되면 바로 stale해진다.
  // 실제 목록은 `agent-flow-kit run next` / `status`가 매번 resolver로 다시 만든다.
  const skillNote = "\n\n## Required skills\n\nRun `agent-flow-kit run next` for the resolved skill list for this phase — it is computed live, not baked here.\n";
  return `# ${phase.id}\n\n${phase.instruction}${markers}${skillNote}\n\nSave the required artifact before running:\n\n\`\`\`bash\n${AGENT_FLOW_COMMAND} run advance\n\`\`\`\n`;
}







// 관리 대상에서 빠진 hook은 기존 설치본 settings에 그대로 남는다. mergeHookSettings는
// additive-only라서, 스크립트 파일만 지우면 host가 없는 경로를 계속 실행해 셸이 통째로
// 막힌다. 은퇴한 이름을 여기 남겨 install 때 등록에서 걷어낸다.
// 관측 hook(`record-*`)은 PostToolUse, 강제 hook은 PreToolUse. 이 구분을 어기면
// 관측자가 판정자로 승격되고, 스크립트가 없거나 죽는 순간 host가 그걸 차단으로
// 읽어 사용자 도구가 통째로 막힌다. 런 시작 시 `core/hook_integrity.py`가
// 이 목록과 배치를 kit.json 기록과 대조한다.

// hook을 끄면 "관리 대상 전부를 은퇴시킨 것"과 같다. 별도 제거 경로를 새로
// 만들지 않고 이미 검증된 prune 경로를 그대로 태운다.















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
}


function installClaudeHooks(root) {
  const settingsPath = path.join(root, ".claude", "settings.json");
  const settings = readHookSettings(settingsPath);
  mergeHookSettings(settings, claudeHooksSettings(root).hooks, hooksDisabled);
  // host가 읽는 사용자 설정이다. 링크를 따라간다.
  atomicWriteFileSync(settingsPath, `${JSON.stringify(settings, null, 2)}\n`, { followSymlink: true });
}



function upgradeBundledProfiles(root, src, dest, keepNames) {
  if (!fs.existsSync(src)) {
    return;
  }
  fs.mkdirSync(dest, { recursive: true });
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
  }
  pruneUninstalledProfiles(root, src, dest, keepNames);
}

// managed hook은 digest가 `kit.json`에 기록되고 run 시작이 그 일치를 요구한다
// (`hook_integrity`). "내용이 다르면 덮지 않는다"만 걸어 두면 kit 업그레이드가
// 정의상 내용이 다른 경우라 digest는 새 값, 파일은 옛 값으로 갈라져 run이 막힌다.
// profile과 같은 취급을 한다 — 덮되 사본을 남긴다.
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






function installOmpHooks(root) {
  // 다른 host의 hook 설정은 병합이라 업그레이드가 저절로 되지만, 이 확장은
  // 통째로 kit이 만든 파일이다. "내용이 다르면 덮지 않는다"만 걸어 두면
  // 업그레이드가 정의상 내용이 다른 경우라 영구히 고착된다.
  const target = path.join(root, ".omp", "extensions", "agent-flow-hooks.ts");
  if (!ompExtensionIsKitOwned(target) && !forceManaged) {
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
  return writeManagedFileIfMissingOrSame(target, ompHooksExtensionSource(), true);
}

function pruneManagedHookRegistrations(root) {
  // 등록만 걷어낸다. hook 스크립트 자체는 pruneRetiredHookScripts가 치운다.
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
      continue;  // 사용자 파일이 깨져 있으면 건드리지 않는다.
    }
    if (pruneRetiredHooks(settings, false, hooksDisabled)) {
      // 위 세 경로는 전부 host가 읽는 사용자 설정이다. 링크를 따라간다.
      atomicWriteFileSync(target, `${JSON.stringify(settings, null, 2)}\n`, { followSymlink: true });
      console.log(`  - hooks disabled: cleared ${path.join(...rel)}`);
    }
  }
}








function workflowContract() {
  return `# Workflow Contract

The workflow runner is the source of truth for phase order. Agents may read skills and prompts, but must follow the runner's printed \`next_command\` exactly to move through the workflow.

Phases with completion markers are not complete just because the artifact file exists. The artifact must include every required marker printed by the current phase or status output.

Implementation rules:

- Run every phase through the runner. Do not skip review, QA, PR watch, or fix-loop phases.
- Apply \`code-generation-discipline\` during red, green, refactor, fix-loop, and review phases. Resolve required skills from active profile metadata, installed skill index, changed files, and task scope before writing or judging code.
- If review or QA fails, return to the fix phase before continuing.
- Required review happens before completion QA. Do not run \`agent-flow gates\` as part of a phase and do not author \`gate-results.json\`: the runner runs the profile gates itself at the gates phase, at \`--phase all\`, and writes the result file. A result file you write is discarded. \`agent-flow gates\` remains available for reproducing a failure by hand. If review or QA fails, fix-loop routes back through comment-authoring and review before gates run again.
- Code review requires at least two independent Claude/Codex reviewer subprocesses. If the changed scope spans multiple areas, run one additional reviewer subprocess in parallel. OMP and controller-session work are never reviewer providers, and every multi-review verdict requires 2+ independent reviewer verdicts with reviewer-source: sub-agent. End multi-review artifacts with ## Overall followed by exactly one verdict line: verdict: approve or verdict: request-changes.
- In the default workflow, gates run as their own runner-owned phase after final-review approve. Per-gate limits come from the active profile's \`gates[].timeout_s\`.

Document size rules:

- domain-grill outputs, compact domain maps, and long planning docs must stay under 200 lines each.
- If a source doc grows past 200 lines, create or refresh a matching \`*-summary.md\` under \`.Codex/rules/\` and use that summary as agent context.
- Preserve the original long doc only as reference; do not load it as hot context unless the current phase needs a specific section.
- Artifacts must link to long docs by repo-relative path and summarize only the needed decision, not paste the full content.

Context rules:

- Artifacts and manifests must use repo-relative paths; local absolute paths are forbidden.
- Do not paste full docs or raw logs into artifacts. Summarize and link by relative path.
- \`CONTEXT.md\` is hot context only.
- Current and future vocabulary must stay separated.
- Follow the phase context map in \`.Codex/rules/context/\` for phase-specific context loading.
- User-facing agent-flow replies must be short and in the language the user writes in. Keep code, commands, paths, and identifiers in English.
- Summarize only current phase, action, \`next_command\`, and blocker when useful.
`;
}

function runArchitectureLint(args) {
  runPythonCliCommand("architecture-lint", args);
}

function runGates(args) {
  runPythonCliCommand("gates", args);
}

function pythonCliEnv() {
  const root = resolveAgentFlowRoot(process.cwd());
  const pythonPathEntries = [
    root ? installedPythonRuntimePath(root) : "",
    path.join(KIT_ROOT, "src"),
    process.env.PYTHONPATH,
  ].filter(Boolean);
  return {
    ...process.env,
    PYTHONPATH: [...new Set(pythonPathEntries)].join(path.delimiter),
  };
}

// 판정을 Python에 묻는다. 지문 검증을 두 언어로 구현하면 갈라지고, 갈라진 쪽이
// 느슨하면 그쪽이 유효한 우회로가 된다.
function pythonCliOutput(subcommand, args, { cwd = process.cwd() } = {}) {
  const result = safeSpawnSync(
    preferredPython(),
    ["-m", "agent_flow.cli", subcommand, ...args],
    {
      cwd,
      env: pythonCliEnv(),
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
      timeout: 30000,
    },
  );
  if (result.error || result.status !== 0) {
    return null;
  }
  return String(result.stdout ?? "").trim() || null;
}

function runPythonCliCommand(subcommand, args, { interactive = false } = {}) {
  const env = pythonCliEnv();
  // gates는 프로파일 게이트 전체를 순차로 돌린다. 이 저장소에서 가장 비싼 게이트는
  // `pytest -q`로 실측 5분대다. relay용 30초 상한을 걸면 정상 실행이 끝나기 전에
  // 죽는다. 개별 게이트 상한은 Python `--timeout`이 소유하고, wrapper는 그 총량만
  // 감싼다. interactive는 사람이 타이핑하는 동안 기다려야 하므로 상한 자체가 없다.
  const result = safeSpawnSync(
    preferredPython(),
    ["-m", "agent_flow.cli", subcommand, ...args],
    {
      // Python CLI는 `--root` 기본값 "."을 여기서 받는다. 그 뒤 leader/git-common
      // root로 옮겨 가므로 cwd는 최종 기준이 아니라 그 해석의 출발점이다.
      cwd: process.cwd(),
      env,
      encoding: "utf8",
      // 승인 문구는 사람에게서 직접 받는다. stdin을 끊으면 TTY 검사에서 죽는다.
      // relay로 도는 명령은 반대로 stdin을 끊어야 입력 대기로 멈추지 않는다.
      stdio: interactive ? "inherit" : ["ignore", "inherit", "inherit"],
      timeout: interactive ? 0 : relayTimeoutForSubcommand(subcommand, args),
    },
  );
  if (result.error) {
    throw result.error;
  }
  process.exit(result.status ?? 1);
}

function installedPythonRuntimePath(root) {
  const runtimePath = path.join(root, RUNTIME_PYTHON_RELATIVE);
  return fs.existsSync(path.join(runtimePath, "agent_flow", "__init__.py")) ? runtimePath : "";
}

try {
  if (command === "install" || (command === "run" && process.argv[3] === "install")) {
    // `run install`은 첫 토큰이 서브커맨드 이름이다. 검증에 그대로 넘기면 그 예외를
    // 위치와 무관하게 허용해야 하고, 그러면 `install install`이 통과한다.
    const args = command === "run" ? installArgs.slice(1) : installArgs;
    if (installHelpRequested(args)) {
      console.log(`usage: ${INSTALL_SYNOPSIS}`);
      process.exit(0);
    }
    assertKnownInstallArgs(args);
    installProject(requestedInstallRoot());
    process.exit(0);
  }

  if (command === "run") {
    runWorkflowCommand(process.argv.slice(3));
    process.exit(0);
  }

  if (command === "architecture-lint") {
    runArchitectureLint(process.argv.slice(3));
  }

  if (command === "gates") {
    runGates(process.argv.slice(3));
  }

  // 래퍼가 모르는 명령은 Python CLI가 주인이다. `spec confirm`, `status`,
  // `continue`처럼 워크플로 프롬프트와 AGENTS.md가 안내하는 명령들이 여기로 온다.
  // 예전엔 전부 usage만 뱉었고, 사용자는 인터프리터와 PYTHONPATH를 손으로 찾다가
  // PyYAML 없는 python을 타고 ModuleNotFoundError로 끝났다.
  if (command) {
    runPythonCliCommand(command, process.argv.slice(3), { interactive: true });
  }

  console.error("usage: agent-flow-kit <command> [...] — install [--root <existing dir>] [--force-managed] | gates [--profile <id>] [--phase <pre-commit|pre-push|post-merge|all>] [--worktree <name>] | architecture-lint [--profile <id>] [--files ...] | run <install|start|status|next|advance|push-watch|push-watch-tick> | any other command is handled by the Python CLI (spec, status, continue, skills, ...)");
  process.exit(1);
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
}
