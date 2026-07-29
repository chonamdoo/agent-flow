#!/usr/bin/env node

import fs from "node:fs";
import crypto from "node:crypto";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { spawnSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { fileURLToPath } from "node:url";
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
const GENERATED_PROJECT_SKILL_NAMES = new Set([
  "architecture-reviewer",
  "full-feature-workflow",
  "plan-reviewer",
  "product-brief",
  "push-watch",
]);
function installProject() {
  const requestedRoot = process.cwd();
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
  const agentFlowSkill = agentFlowSkillMarkdown();
  writeManagedFile(path.join(agentFlowDir, "skills", "agent-flow", "SKILL.md"), agentFlowSkill);
  writeManagedFile(
    path.join(agentFlowDir, "skills", "full-feature-workflow", "SKILL.md"),
    fullFeatureSkillMarkdown(),
  );
  writeManagedFile(path.join(agentFlowDir, "skills", "product-brief", "SKILL.md"), productBriefSkillMarkdown());
  writeManagedFile(path.join(agentFlowDir, "skills", "plan-reviewer", "SKILL.md"), planReviewerSkillMarkdown());
  writeManagedFile(
    path.join(agentFlowDir, "skills", "architecture-reviewer", "SKILL.md"),
    architectureReviewerSkillMarkdown(),
  );
  writeManagedFile(path.join(agentFlowDir, "skills", "push-watch", "SKILL.md"), pushWatchSkillMarkdown());
  copyBundledDirIfMissingOrSame(
    path.join(KIT_ROOT, "skills"),
    path.join(agentFlowDir, "skills"),
    forceManaged,
    PROFILE_MANAGED_HOST_ONLY_SKILLS,
    true,
    forceManaged,
    new Set(["index.json", "catalog.lock.json", ...GENERATED_PROJECT_SKILL_NAMES]),
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
  // 으로 조용히 내려앉는다(`runner.py` `_load_single_profile`). 위 `.agent-flow/profiles/`
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
  writeManagedFile(path.join(agentFlowDir, "prompts", "push-watch.md"), pushWatchPromptMarkdown());
  writeManagedFile(path.join(agentFlowDir, "prompts", "push-watch-tick.md"), pushWatchTickPromptMarkdown());
  for (const phase of phases) {
    writeManagedFile(
      path.join(agentFlowDir, "prompts", `${phase.id}.md`),
      phasePrompt(phase),
    );
  }
  writeManagedFile(path.join(agentFlowDir, "rules", "workflow-contract.md"), workflowContract());
  writeManagedFile(path.join(agentFlowDir, "bootstrap", "AGENTS.md"), bootstrapMarkdown("AGENTS.md"));
  writeManagedFile(path.join(agentFlowDir, "bootstrap", "CLAUDE.md"), bootstrapMarkdown("CLAUDE.md"));
  const gitignorePath = path.join(root, ".gitignore");
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
  removeLegacyProjectSkillCopies(root, "graphify");
  upsertBootstrapBlock(path.join(root, "AGENTS.md"), "AGENTS.md", root);
  upsertBootstrapBlock(path.join(root, "CLAUDE.md"), "CLAUDE.md", root);
  upsertSkillIndexBlock(root);
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

  fs.writeFileSync(path.join(agentFlowDir, "kit.json"), `${JSON.stringify(payload, null, 2)}\n`, "utf8");
  syncSkillSources(root);
  console.log(`agent-flow installed profile=${profile}`);
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
    const state = readCurrentRun(root);
    if (state.phase !== "pr-watch") {
      throw new Error(`blocked: push-watch-tick requires current phase pr-watch, got ${state.phase}`);
    }
    const runDir = resolveRunDir(root, state.run_dir);
    const pr = readPullRequestStatus(process.cwd());
    const watchStatus = pullRequestWatchStatus(pr);
    const artifact = path.join(runDir, "artifacts", "pr-watch.md");
    writeManagedFile(
      artifact,
      [`status: ${watchStatus}`, `pr: ${pr.url ?? "unknown"}`, `recorded_at: ${new Date().toISOString()}`, ""].join("\n"),
    );
    const previous = fs.existsSync(pushWatchStatePath(root))
      ? JSON.parse(fs.readFileSync(pushWatchStatePath(root), "utf8"))
      : {};
    writeJson(pushWatchStatePath(root), {
      ...previous,
      status: watchStatus,
      pr: pr.url ?? null,
      iterations: Number(previous.iterations ?? 0) + 1,
      updated_at: new Date().toISOString(),
    });
    console.log(`push-watch status=${watchStatus}`);
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
  const identityArgs = [
    ...rootedArgs,
    "--checkout-identity",
    checkoutIdentity,
  ];
  if (subcommand === "start") {
    const extracted = extractCliOption(identityArgs, "--workflow");
    runPythonCliCommand(
      "start",
      [
        extracted.value ?? "full-feature",
        ...extracted.args,
        "--phase-runner",
      ],
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

function cliOptionValue(args, name) {
  let value;
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === name) {
      if (index + 1 >= args.length) {
        throw new Error(`${name} requires a value`);
      }
      if (value !== undefined) {
        throw new Error(`${name} may only be specified once`);
      }
      value = args[index + 1];
      index += 1;
    } else if (arg.startsWith(`${name}=`)) {
      if (value !== undefined) {
        throw new Error(`${name} may only be specified once`);
      }
      value = arg.slice(name.length + 1);
    }
  }
  return value;
}

function extractCliOption(args, name) {
  const kept = [];
  let value;
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === name) {
      if (index + 1 >= args.length) {
        throw new Error(`${name} requires a value`);
      }
      if (value !== undefined) {
        throw new Error(`${name} may only be specified once`);
      }
      value = args[index + 1];
      index += 1;
    } else if (arg.startsWith(`${name}=`)) {
      if (value !== undefined) {
        throw new Error(`${name} may only be specified once`);
      }
      value = arg.slice(name.length + 1);
    } else {
      kept.push(arg);
    }
  }
  return { value, args: kept };
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

// Python `git_safe`(`core/worktree_isolation.py`)와 같은 목적이다. 오염된 ambient
// GIT_DIR/GIT_WORK_TREE가 우리 git을 요청한 cwd 밖으로 돌리면 JS와 Python이 서로
// 다른 root를 고른다.
const GIT_DISCOVERY_ENV = [
  "GIT_DIR",
  "GIT_WORK_TREE",
  "GIT_COMMON_DIR",
  "GIT_INDEX_FILE",
  "GIT_OBJECT_DIRECTORY",
  "GIT_ALTERNATE_OBJECT_DIRECTORIES",
  "GIT_CEILING_DIRECTORIES",
  "GIT_NAMESPACE",
  "GIT_PREFIX",
];

function gitEnv() {
  const env = { ...process.env };
  for (const name of GIT_DISCOVERY_ENV) {
    delete env[name];
  }
  // 분기 대상 메시지를 결정적으로 유지한다. 이 명령에만 적용된다.
  env.LC_ALL = "C";
  env.LANG = "C";
  return env;
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

function resolveInstallRoot(start) {
  const worktreeRoot = resolveManagedWorktreeRoot(start);
  if (worktreeRoot) {
    return worktreeRoot;
  }
  const gitCommonRoot = resolveGitCommonWorktreeRoot(start);
  if (gitCommonRoot) {
    return gitCommonRoot;
  }
  const parts = start.split(path.sep);
  const markerIndex = parts.lastIndexOf(".agent-flow");
  if (markerIndex !== -1) {
    return parts.slice(0, markerIndex).join(path.sep) || path.sep;
  }
  return start;
}

function resolveManagedWorktreeRoot(start) {
  return resolveManagedWorktreeContext(start)?.root ?? null;
}

function resolveManagedWorktreeContext(start) {
  const resolved = canonicalPath(start);
  const parts = resolved.split(path.sep);
  const markers = new Set([".agent-flow", ".codex", ".Codex", ".omp"]);
  for (let index = parts.length - 3; index >= 0; index -= 1) {
    if (parts[index + 1] !== "worktrees") continue;
    if (!markers.has(parts[index])) continue;
    const root = parts.slice(0, index).join(path.sep) || path.sep;
    if (
      HOME
      && samePath(root, HOME)
      && [".codex", ".Codex", ".omp"].includes(parts[index])
    ) {
      continue;
    }
    return { root, name: parts[index + 2] };
  }
  return null;
}

function canonicalPath(value) {
  try {
    return fs.realpathSync.native(value);
  } catch {
    return path.resolve(value);
  }
}

function samePath(left, right) {
  try {
    return fs.realpathSync.native(left) === fs.realpathSync.native(right);
  } catch {
    // 심볼릭 링크가 섞인 임시 경로에서도 홈 비교는 보수적으로 처리한다.
    return path.resolve(left) === path.resolve(right);
  }
}


function currentCheckoutIdentity(root) {
  const cwd = canonicalPath(process.cwd());
  const managed = resolveManagedWorktreeContext(cwd);
  if (managed && samePath(managed.root, root)) {
    return `worktree:${managed.name}`;
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

function resolveGitCommonWorktreeRoot(start) {
  const topLevel = gitOutput(start, ["rev-parse", "--show-toplevel"]);
  const commonDir = gitOutput(start, ["rev-parse", "--git-common-dir"]);
  if (!topLevel || !commonDir) {
    return null;
  }
  const resolvedCommonDir = path.resolve(start, commonDir);
  if (path.basename(resolvedCommonDir) !== ".git") {
    return null;
  }
  return path.dirname(resolvedCommonDir);
}

function gitOutput(cwd, args) {
  const result = safeSpawnSync("git", args, {
    cwd,
    env: gitEnv(),
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
  if (result.error) {
    // git을 실행조차 못 했다. 바이너리가 없으면(ENOENT) 대안이 경로 스캔뿐이지만,
    // 타임아웃처럼 "돌긴 했는데 대답을 못 받은" 경우까지 조용히 넘기면 어느
    // 저장소인지 모르는 채로 계속 간다.
    if (result.error.code === "ENOENT") {
      return null;
    }
    throw new Error(`git ${args.join(" ")} failed: ${result.error.message}`);
  }
  if (result.status !== 0) {
    // 비영 종료는 "여기는 그 질문에 답할 수 있는 저장소가 아니다"다. 저장소 아님,
    // bare repo, 열 수 없는 repo format, dubious ownership이 전부 여기로 온다.
    // 이걸 오류로 올리면 non-git 디렉터리에서 install조차 못 한다. 호출자의 폴백은
    // `.agent-flow/kit.json`이 실제로 있는 자리만 고르는 앵커된 탐색이라 안전하다.
    return null;
  }
  const output = result.stdout.trim();
  return output || null;
}

const DEFAULT_RELAY_TIMEOUT_MS = 30_000;
// gates는 프로파일 게이트를 순차로 돌린다. 개별 게이트 상한은 Python `--timeout`이
// 소유하므로 wrapper는 그 총량만 감싼다. 무제한으로 두면 python이 시작조차 못 한
// 경우 전경이 영영 멈춘다.
const DEFAULT_GATE_TIMEOUT_S = 600;
// `--profile a,b`와 kit.json의 profiles 리스트는 여러 프로파일의 게이트를 합집합으로
// 돌린다(`src/agent_flow/cli.py:_profile_gate_commands`). 배포 프로파일 전부를 켠
// 실측 최대가 20개라 그보다 위에서 자른다.
const MAX_TOTAL_GATES = 24;
const GATE_RELAY_SLACK_MS = 120_000;

function gateTimeoutSeconds(args) {
  // argparse는 `--timeout 900`, `--timeout=900`, 유일 접두 축약(`--time 900`)을
  // 모두 받는다. 한 형태만 읽으면 사용자가 올린 상한이 예산에 반영되지 않는다.
  const isTimeoutFlag = (value) =>
    value.length >= 3 && "--timeout".startsWith(value);
  let selected = DEFAULT_GATE_TIMEOUT_S;
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

// architecture-lint는 profile gate로도 돌고(그때는 gate 예산 안에 있다) node로
// 직접도 불린다. 직접 호출만 30초로 잘리면 같은 명령이 호출 경로에 따라 다르게
// 죽는다. Python 파서에 `--timeout`이 없으므로 게이트 하나치 기본 예산을 준다.
const SINGLE_GATE_RELAY_TIMEOUT_MS = DEFAULT_GATE_TIMEOUT_S * 1000 + GATE_RELAY_SLACK_MS;

function relayTimeoutForSubcommand(subcommand, args) {
  if (subcommand === "gates") {
    return gateTimeoutSeconds(args) * 1000 * MAX_TOTAL_GATES + GATE_RELAY_SLACK_MS;
  }
  if (subcommand === "architecture-lint") {
    return SINGLE_GATE_RELAY_TIMEOUT_MS;
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
  const managed = resolveManagedWorktreeContext(process.cwd());
  const requestedName = worktree || (
    managed && samePath(managed.root, root)
      ? managed.name
      : null
  );
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

function writeJson(pathName, payload) {
  fs.mkdirSync(path.dirname(pathName), { recursive: true });
  fs.writeFileSync(pathName, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
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

function writeManagedFile(pathName, content) {
  fs.mkdirSync(path.dirname(pathName), { recursive: true });
  fs.writeFileSync(pathName, content, "utf8");
}

function writeManagedFileIfMissingOrSame(pathName, content, force = false) {
  fs.mkdirSync(path.dirname(pathName), { recursive: true });
  if (fs.existsSync(pathName)) {
    const current = fs.readFileSync(pathName, "utf8");
    if (force) {
      fs.writeFileSync(pathName, content, "utf8");
      return true;
    }
    if (current !== content) {
      return false;
    }
    return true;
  }
  fs.writeFileSync(pathName, content, "utf8");
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
  const index = { ...selected, links };
  fs.writeFileSync(
    path.join(agentFlowDir, "skills", "index.json"),
    `${JSON.stringify(index, null, 2)}\n`,
    "utf8",
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
    const metadata = parseSkillMetadata(text, entry.name);
    if (ignoredNames.has(metadata.name)) {
      continue;
    }
    const relativePath = path.relative(root, skillPath);
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
  if (name !== parsedName) {
    warnings.push(`unsafe skill name ignored: ${parsedName}`);
  }
  const hostValues = Array.isArray(metadata.hosts) ? metadata.hosts : [];
  const knownHosts = new Set(PROJECT_SKILL_HOSTS);
  const hosts = [];
  for (const host of hostValues) {
    const normalized = String(host).trim().toLowerCase();
    if (knownHosts.has(normalized)) {
      hosts.push(normalized);
    } else if (normalized) {
      warnings.push(`unknown host ignored: ${normalized}`);
    }
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

function splitSkillFrontmatter(text) {
  if (!text.startsWith("---\n")) {
    return null;
  }
  const end = text.indexOf("\n---\n", 4);
  if (end === -1) {
    return null;
  }
  return text.slice(4, end);
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
    const key = match[1];
    const raw = match[2].trim();
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




function pathHasSymlink(root, target) {
  const relative = path.relative(root, target);
  const parts = relative.split(path.sep).filter(Boolean);
  let cursor = root;
  for (const part of parts) {
    cursor = path.join(cursor, part);
    const stat = lstatIfExists(cursor);
    if (stat && stat.isSymbolicLink()) {
      return true;
    }
  }
  return false;
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
  try {
    const content = fs.readFileSync(pathName, "utf8");
    const data = JSON.parse(content);
    if (typeof data.passed !== "boolean" || !Array.isArray(data.results) || data.results.length === 0) {
      if (typeof data.passed === "boolean" && typeof data.status === "string") {
        const status = data.status.trim().toLowerCase().replace(/_/g, "-");
        if (data.passed === false && ["request-changes", "blocked", "error", "pending"].includes(status)) {
          return status;
        }
      }
      return typeof data.passed === "boolean" && data.passed === false ? "request-changes" : "default";
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


function upsertBootstrapBlock(pathName, label, root) {
  const start = "<!-- agent-flow:start -->";
  const end = "<!-- agent-flow:end -->";
  const block = `${start}
## Agent Flow

Before feature work, check status first:

\`\`\`bash
${AGENT_FLOW_COMMAND} status
\`\`\`

install은 프로젝트당 1회만 수행합니다. 새 세션이 시작됐다는 이유로 install을 다시 실행하지 않습니다.
Follow the CLI output exactly. If no run is active, start with \`${AGENT_FLOW_COMMAND} run "<task>"\`. If a run is active, continue with the printed \`next_command\`.

run이 SPEC 확인 대기로 막히면 지원되는 Codex·Claude·OMP host에서는 사용자에게 현재 대화의 새 turn으로 정확히 \`승인\`이라고 답해 달라고 안내합니다. managed user-prompt hook이 현재 pending SPEC을 확인합니다. hook을 사용할 수 없을 때만 사용자가 대상 worktree의 대화형 터미널에서 경로 없는 fallback \`${AGENT_FLOW_COMMAND} spec confirm\`을 직접 실행합니다.
\`manual\` verify 항목은 사용자가 \`${AGENT_FLOW_COMMAND} spec approve <spec-id> --run-dir <run-dir>\`를 실행해야 승인 record가 남습니다.
agent는 fallback·manual 승인 명령이나 user-prompt hook을 대신 실행하지 않습니다. agent 셸에서 관측된 확인·승인은 무효 처리됩니다.

### Workflow Contract

- 활성 workflow와 current phase는 항상 \`${AGENT_FLOW_COMMAND} status\` 출력 기준이다.
- phase 이동은 status의 \`next_command\`를 그대로 따른다. \`${AGENT_FLOW_COMMAND} continue\`나 \`${AGENT_FLOW_COMMAND} run advance\`를 추측하지 않는다.
- \`default.yaml\`: design → slice-plan → worktree → implement → comment-authoring → final-review → gates ↔ fix-loop → comment-authoring → final-review → gates → commit → push-pr → pr-watch ↔ pr-comment-fix/pr-ci-fix → merge → cleanup
- \`full-feature.yaml\`: domain-grill → product-brief → prd → slice-plan → plan-review → ddd-design → worktree → run-start → red → green → refactor → comment-authoring → multi-review → architecture-review → gates ↔ fix-loop → comment-authoring → multi-review → architecture-review → gates → commit → push-pr → pr-watch ↔ pr-comment-fix/pr-ci-fix → merge-approval → merge → handoff
- \`multi-review\`는 현재 사용 중인 CLI(활성 host)의 sub-agent 2개가 필수다. 두 sub-agent를 병렬 실행하고, \`reviewer-source: sub-agent\`를 기록한 뒤 sub-agent를 닫는다. 마지막에 \`## Overall\`과 \`verdict: approve\` 또는 \`verdict: request-changes\`만 기록한다. 활성 host가 아닌 추가 provider는 optional이다.

### Context Economy

- Claude/Codex/OMP user-facing 답변은 기본적으로 짧은 한글로 한다.
- 코드/명령/식별자는 영어 그대로 유지한다.
- 긴 설명, 긴 로그, 전체 파일 붙여넣기 금지.
- 필요한 경우만 current phase, action, \`next_command\`, blocker를 요약한다.
- 모든 guide를 항상 로드하지 말고 변경 파일에 필요한 guide만 읽는다.
- 프로젝트 skill은 \`skills/<name>/SKILL.md\` 또는 private \`.agent-flow/local-skills/<name>/SKILL.md\`에 둔다.
<!-- agent-flow:skills:start -->
- 설치된 skill 인덱스가 아직 없다. install이 이 자리에 채운다.
<!-- agent-flow:skills:end -->
- 인덱스에 없는 skill은 이 프로젝트에 설치돼 있지 않다. 런타임에 설치를 묻지 말고 \`${AGENT_FLOW_COMMAND} skills sync\`에 맡긴다.
- Claude/Codex/OMP 프로젝트 skill 경로는 leader checkout의 install 결과를 따른다. worktree 안에서 install, index 재생성, skill link 재생성을 하지 않는다.
- Claude/Codex/OMP hook이 자동 차단하는 보호 브랜치 commit/push와 leader checkout/switch 금지는 모든 host에서 동일하게 지킨다.
${end}
`;
  const current = fs.existsSync(pathName) ? fs.readFileSync(pathName, "utf8") : "";
  if (current.includes(start) && current.includes(end)) {
    const before = current.slice(0, current.indexOf(start));
    const after = current.slice(current.indexOf(end) + end.length);
    fs.writeFileSync(pathName, `${before}${block}${after.replace(/^\n/, "")}`, "utf8");
    return;
  }
  const prefix = current.trim() ? `${current.trimEnd()}\n\n` : `# ${label}\n\n`;
  fs.writeFileSync(pathName, `${prefix}${block}`, "utf8");
}









function bootstrapMarkdown(label) {
  return `# ${label} Agent Flow Bootstrap

Before feature work, run:

\`\`\`bash
${AGENT_FLOW_COMMAND} run "<task>"
\`\`\`

install은 프로젝트당 1회만 수행합니다. 새 세션이 시작됐다는 이유로 install을 다시 실행하지 않습니다.
Follow the CLI output exactly. Git projects start inside \`.agent-flow/worktrees/feat-<slug>/\` without switching the leader branch; continue with the printed \`next_command\`.

run이 SPEC 확인 대기로 막히면 지원되는 Codex·Claude·OMP host에서는 사용자에게 현재 대화의 새 turn으로 정확히 \`승인\`이라고 답해 달라고 안내합니다. managed user-prompt hook이 현재 pending SPEC을 확인합니다. hook을 사용할 수 없을 때만 사용자가 대상 worktree의 대화형 터미널에서 경로 없는 fallback \`${AGENT_FLOW_COMMAND} spec confirm\`을 직접 실행합니다.
\`manual\` verify 항목은 사용자가 \`${AGENT_FLOW_COMMAND} spec approve <spec-id> --run-dir <run-dir>\`를 실행해야 승인 record가 남습니다.
agent는 fallback·manual 승인 명령이나 user-prompt hook을 대신 실행하지 않습니다. agent 셸에서 관측된 확인·승인은 무효 처리됩니다.

### Workflow Contract

- 활성 workflow와 current phase는 항상 \`${AGENT_FLOW_COMMAND} status\` 출력 기준이다.
- phase 이동은 status의 \`next_command\`를 그대로 따른다. \`${AGENT_FLOW_COMMAND} continue\`나 \`${AGENT_FLOW_COMMAND} run advance\`를 추측하지 않는다.
- \`default.yaml\`: design → slice-plan → worktree → implement → comment-authoring → final-review → gates ↔ fix-loop → comment-authoring → final-review → gates → commit → push-pr → pr-watch ↔ pr-comment-fix/pr-ci-fix → merge → cleanup
- \`full-feature.yaml\`: domain-grill → product-brief → prd → slice-plan → plan-review → ddd-design → worktree → run-start → red → green → refactor → comment-authoring → multi-review → architecture-review → gates ↔ fix-loop → comment-authoring → multi-review → architecture-review → gates → commit → push-pr → pr-watch ↔ pr-comment-fix/pr-ci-fix → merge-approval → merge → handoff
- \`multi-review\`는 현재 사용 중인 CLI(활성 host)의 sub-agent 2개가 필수다. 두 sub-agent를 병렬 실행하고, \`reviewer-source: sub-agent\`를 기록한 뒤 sub-agent를 닫는다. 마지막에 \`## Overall\`과 \`verdict: approve\` 또는 \`verdict: request-changes\`만 기록한다. 활성 host가 아닌 추가 provider는 optional이다.

### Context Economy

- Claude/Codex/OMP user-facing 답변은 기본적으로 짧은 한글로 한다.
- 코드/명령/식별자는 영어 그대로 유지한다.
- 긴 설명, 긴 로그, 전체 파일 붙여넣기 금지.
- 필요한 경우만 current phase, action, \`next_command\`, blocker를 요약한다.
- 모든 guide를 항상 로드하지 말고 변경 파일에 필요한 guide만 읽는다.
- 프로젝트 skill은 \`skills/<name>/SKILL.md\` 또는 private \`.agent-flow/local-skills/<name>/SKILL.md\`에 둔다.
- install/bootstrap 후 \`.agent-flow/skills/index.json\` metadata를 보고 필요한 skill만 읽는다. 모든 SKILL.md 전문을 항상 읽지 않는다.
- Claude/Codex/OMP 프로젝트 skill 경로는 leader checkout의 install 결과를 따른다. worktree 안에서 install, index 재생성, skill link 재생성을 하지 않는다.
- Claude/Codex/OMP hook이 자동 차단하는 보호 브랜치 commit/push와 leader checkout/switch 금지는 모든 host에서 동일하게 지킨다.

During code generation, modification, and code review phases, apply \`code-generation-discipline\`. Resolve required skills from active profile metadata, installed skill index, changed files, and task scope. Load only the touched profile skill union.
The phase prompt lists the skills for this change — required ones with paths, in-scope ones by name. That list is resolved per run from the active profile's skill vocabulary, the skills installed on this machine, the changed files, and the task text. Read the required ones; call the in-scope ones by name only when the change actually touches them. No configuration enumerates installed external skill names: vocabulary is declared instead, so an upstream add, delete, or rename needs no edit here. If a required local skill is missing, report \`missing local <group>: <skill>\` with the profile source URL and ask the user to install it.
`;
}

function newRunId() {
  const stamp = new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
  return `${stamp}-${Math.random().toString(16).slice(2, 10)}`;
}

function fullFeatureWorkflowYaml() {
  return fullFeatureWorkflow().text;
}

function agentFlowSkillMarkdown() {
  return `---
name: agent-flow
description: Use when the user types /agent-flow, asks to start or continue the project workflow, or wants Claude, Codex, or OMP to drive the agent-flow lifecycle.
---

# Agent Flow

Use this skill as the common entry point for the project-local agent-flow workflow.

## Slash Trigger

When the user types \`/agent-flow <task>\`, run:

\`\`\`bash
${AGENT_FLOW_COMMAND} run "<task>"
\`\`\`

Do not reinstall agent-flow for each task. Install is project setup, not the normal task entry.
In a git repo, \`${AGENT_FLOW_COMMAND} run "<task>"\` starts the run inside \`.agent-flow/worktrees/feat-<slug>/\` on branch \`feat/<slug>\`.

When the user types \`/agent-flow\` with no task:

- Run \`${AGENT_FLOW_COMMAND} status\` from the project root.
- Treat the status command output as the only source of truth.
- If status exits 0 and reports an active run, follow the \`next_command\` from status.
- If status exits non-zero with \`no active run\`, ask for a task using \`/agent-flow <task>\`.
- Do not infer npm, npx, or install failure unless the command actually exits non-zero with that error.
- Do not run install just because a new session started.

When the user types \`/agent-flow status\`, run:

\`\`\`bash
${AGENT_FLOW_COMMAND} status
\`\`\`

## User-Only Approval

When a run waits for SPEC set confirmation:

- On a supported Codex, Claude, or OMP host, show the complete ordered SPEC list and ask the user to reply exactly \`승인\` in a new turn of the current chat. The managed user-prompt hook confirms the current pending SPEC.
- Only when that hook is unavailable, ask the user to run the path-free fallback \`${AGENT_FLOW_COMMAND} spec confirm\` from the target worktree in an interactive terminal.
- A \`manual\` verifier still requires the user to run \`${AGENT_FLOW_COMMAND} spec approve <spec-id> --run-dir <run-dir>\`.

Never run the fallback or manual approval command, or invoke the user-prompt hook yourself. Confirmation or approval observed from an agent shell is void and the phase stays blocked.

## Behavior

- Treat \`/agent-flow\` as a project-local workflow trigger, not as a shell path.
- Keep git-project runtime state private under the repository git dir, such as \`.git/agent-flow/worktrees/feat-<slug>/\`; expose it only for status, debugging, or artifact inspection.
- On a new session, always check \`${AGENT_FLOW_COMMAND} status\` first and continue from that result.
- After a phase writes its artifact, run the \`next_command\` printed by status or the current phase output.
- If the workflow pauses for design or slice review, summarize the relevant artifact and wait for user approval before continuing.
- During code generation, modification, and code review phases, apply \`code-generation-discipline\`. Resolve required skills from active profile metadata, installed skill index, changed files, and task scope. Load only the touched profile skill union. If a required local skill is missing, report it and wait for install or explicit override.
- Keep user-facing replies short Korean by default. Keep code, commands, paths, and identifiers in English.
- Do not paste long logs or whole files. Summarize only current phase, action, \`next_command\`, and blocker when useful.
`;
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
    fs.mkdirSync(path.dirname(settingsPath), { recursive: true });
    fs.writeFileSync(settingsPath, `${JSON.stringify(settings, null, 2)}\n`, "utf8");
  }
}


function installClaudeHooks(root) {
  const settingsPath = path.join(root, ".claude", "settings.json");
  const settings = readHookSettings(settingsPath);
  mergeHookSettings(settings, claudeHooksSettings(root).hooks, hooksDisabled);
  fs.mkdirSync(path.dirname(settingsPath), { recursive: true });
  fs.writeFileSync(settingsPath, `${JSON.stringify(settings, null, 2)}\n`, "utf8");
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
    backupIfDifferent(root, target, content);
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
    const backup = backupIfDifferent(root, target, content);
    if (backup) {
      // 백업은 hook 디렉터리 안에 남는다. 실행 권한을 그대로 물려주면
      // `hook_integrity`가 관리 대상 아닌 실행 파일로 보고 run 시작을 막는다.
      fs.chmodSync(backup, 0o644);
    }
    fs.writeFileSync(target, content, "utf8");
    fs.chmodSync(target, fs.statSync(source).mode & 0o777);
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
  // kit 소유여도 사용자가 손댔을 수 있다. 서명만으로는 구분이 안 되므로
  // 다른 내용이면 지우기 전에 사본을 남긴다.
  backupIfDifferent(root, target, ompHooksExtensionSource());
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
      fs.writeFileSync(target, `${JSON.stringify(settings, null, 2)}\n`, "utf8");
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
- Required review happens before completion QA. After reviewer approve, run \`agent-flow gates --phase all\`. The CLI default is \`pre-commit\` and build/test gates are declared \`pre-push\`, so only \`--phase all\` runs them; a \`pre-commit\` run is not accepted as QA evidence. If review or QA fails, fix-loop routes back through comment-authoring and review before gates run again.
- Code review requires at least two active-host sub-agents (Codex sub-agent in Codex, Claude sub-agent in Claude, OMP sub-agent in OMP). If the changed scope spans multiple areas, run one additional active-host sub-agent in parallel. Additional non-host providers are optional, and every multi-review verdict requires 2+ independent sub-agent reviewer verdicts with reviewer-source: sub-agent. After recording each sub-agent result, close that sub-agent session. End multi-review artifacts with ## Overall followed by exactly one verdict line: verdict: approve or verdict: request-changes.
- In the default workflow, gates run as their own phase after final-review approve.

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
- User-facing agent-flow replies must be short Korean by default. Keep code, commands, paths, and identifiers in English.
- Summarize only current phase, action, \`next_command\`, and blocker when useful.
`;
}

function runArchitectureLint(args) {
  runPythonCliCommand("architecture-lint", args);
}

function runGates(args) {
  runPythonCliCommand("gates", args);
}

function runPythonCliCommand(subcommand, args, { interactive = false } = {}) {
  const root = resolveAgentFlowRoot(process.cwd());
  const pythonPathEntries = [
    root ? installedPythonRuntimePath(root) : "",
    path.join(KIT_ROOT, "src"),
    process.env.PYTHONPATH,
  ].filter(Boolean);
  const env = {
    ...process.env,
    PYTHONPATH: [...new Set(pythonPathEntries)].join(path.delimiter),
  };
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
  if (command === "install") {
    installProject();
    process.exit(0);
  }

  if (command === "run" && process.argv[3] === "install") {
    installProject();
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

  console.error("usage: agent-flow-kit <command> [...] — install [--force-managed] | gates [--profile <id>] [--phase <pre-commit|pre-push|post-merge|all>] [--worktree <name>] | architecture-lint [--profile <id>] [--files ...] | run <install|start|status|next|advance|push-watch|push-watch-tick> | any other command is handled by the Python CLI (spec, status, continue, skills, ...)");
  process.exit(1);
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
}
