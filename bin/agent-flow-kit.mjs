#!/usr/bin/env node

import fs from "node:fs";
import crypto from "node:crypto";
import path from "node:path";
import process from "node:process";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import {
  SKILL_DEPENDENCIES,
  discoverAutomaticExternalSkillNames,
  isPortableSkillName,
  mergeInstallSelectionWithPrevious,
  mergeResolvedSkillClosure,
  portableSkillCasefold,
  resolveInstallSelection,
  resolveProfileSkillSources,
  resolveRuntimeSkillPlan,
  hashSkillTree,
  validateAndroidOfficialLock,
} from "../lib/skill-selection.mjs";
import { ensureWorktreeHostBridge } from "../lib/worktree-host-bridge.mjs";
import {
  START_LOCK_KEYS,
  START_LOCK_VERSION,
  acquireProjectStartLock,
  projectStartLockPath,
  releaseProjectStartLock,
} from "../lib/start-lock.mjs";
import { semanticWorktreeSlug } from "../lib/worktree-naming.mjs";
import {
  captureExecutionState,
  executionStatePromptBlock,
  initializeExecutionStateLedger,
  observeExecutionStateInjection,
  resolveLedgerMode,
} from "../lib/execution-state-ledger.mjs";
import { detectActiveHost } from "../lib/host-detection.mjs";
import {
  applyCodexConfigTrustUpdates,
  canonicalCodexConfigPath,
  recoverInterruptedCodexTrustTransaction,
  tomlBasicStringInterior as tomlBasicString,
} from "../lib/codex-hook-trust.mjs";
import {
  NODE_RUNTIME_ENTRYPOINT_RELATIVE,
  PYTHON_RUNTIME_ROOT_RELATIVE,
  PROJECT_RUNTIME_CONTRACT_COMMITMENT_VERSION,
  assertProjectRuntimeInstalled,
  buildProjectRuntimeContract,
} from "../lib/project-runtime-contract.mjs";
import {
  createCanonicalProfileLoader,
  gateSummary,
  parseArchitectureLintArgs,
  parseGatesArgs,
  profileGateCommands,
  nativeGateHelpFromArgs,
  requestedProfileIds,
  runArchitectureLint as runNativeArchitectureLint,
  runGates as runNativeGateCommands,
  writeGateResults,
} from "../lib/native-gates.mjs";
import { experimentHelpFromArgs, recordUsageFromArgs } from "../lib/native-experiment.mjs";
import yamlRuntime from "../lib/yaml-runtime-bundled.cjs";

const {
  mergeProfilePayloads,
  parseInstalledProfileYaml,
  parseYamlMapping,
  parseWorkflowYaml,
  renderProfilePromptBlock,
} = yamlRuntime;

process.env.PYTHONDONTWRITEBYTECODE ||= "1";

const command = process.argv[2];
const AGENT_FLOW_COMMAND = "./.agent-flow/bin/agent-flow";
const HOME = process.env.HOME || process.env.USERPROFILE || "";
const KIT_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const RUNTIME_PYTHON_RELATIVE = PYTHON_RUNTIME_ROOT_RELATIVE.split("/").join(path.sep);
const installArgs = process.argv.slice(3);
const forceManaged = installArgs.includes("--force-managed");
let cachedFullFeatureWorkflow = null;
let pendingProjectSkillHostTransaction = null;
const installedProfileSnapshotCache = new Map();
const VERIFIED_RUN_WORKFLOW_PHASES = Symbol("verified-run-workflow-phases");
const PROFILE_ARCHITECTURE_PHASES = new Set([
  "design",
  "ddd-design",
  "architecture-review",
  "slice-plan",
  "plan-review",
]);
const PROJECT_SKILL_HOSTS = Object.freeze(["claude", "codex", "omp"]);
const PYTHON_PROJECT_MARKERS = Object.freeze([
  "pyproject.toml",
  "requirements.txt",
  "requirements-dev.txt",
  "Pipfile",
  "poetry.lock",
  "uv.lock",
]);
const BUNDLED_HOST_SKILL_NAMES = new Set([
  "agent-flow",
  "android-appshell-error-handling",
  "comment-authoring-discipline",
  "comment-checker",
  "ios-app-shell-error-handling",
  "react-app-shell-error-handling",
  "react-native-app-shell-error-handling",
]);
const GENERATED_PROJECT_SKILL_NAMES = new Set([
  "architecture-reviewer",
  "full-feature-workflow",
  "plan-reviewer",
  "product-brief",
  "push-watch",
]);
const RESERVED_CORE_SKILL_NAMES = new Set(["agent-flow", ...GENERATED_PROJECT_SKILL_NAMES]);
const MANAGED_HOST_FILES_VERSION = 1;
const MANAGED_HOST_FILES_COMMITMENT_VERSION = 1;
const SKILL_LINKS_COMMITMENT_VERSION = 1;
const MANAGED_HOOK_CONTRACT_VERSION = 2;
const MANAGED_HOOK_CONTRACT_COMMITMENT_VERSION = 2;
const LEGACY_MIGRATION_BASE_COMMIT = "997cbfcfcaffa2a249c0e7903797e4cf0a7c4a4b";
const LEGACY_SKILL_TREE_COMMITMENT = "5d32e8d1c630749b060c60a9e5b3953a6d6481387df1da5728f1d2f98b49f0fc";
const LEGACY_MANAGED_HOOK_HASHES = Object.freeze({
  ".agent-flow/scripts/hooks/comment-checker.py": "da83ebd6abf0ae1c3987a6381604b631a3a92d29c7ffd2f2cf7bc9dee6761ee5",
  ".agent-flow/scripts/hooks/guard-protected-branch.sh": "716480fe718fd0ca00680cdd7952c955662147b8c4c77aca9668b98f20472b96",
  ".agent-flow/scripts/hooks/guard-worktree.sh": "5c0fe25e08e5edd7653c3fd30cc50b4ee2cb57d881ae0e0a26971f293cb41658",
  ".agent-flow/scripts/hooks/show-phase-status.sh": "bed53c6a74b28751e2a8848fc03de626c812077ee504932b00649c79cdd27ee6",
});
const LEGACY_MANAGED_HOST_HASHES = Object.freeze({
  ".Codex/agents/code-reviewer.md": "0cb649d6a77427ef2835f719d282144d03fe41dda9443f7661433efdba538359",
  ".claude/agents/code-reviewer.md": "8e7a9b62f539f56180dce81d804c23039deff021aa7233aa4f28f1cc1a8551cd",
  ".omp/extensions/agent-flow-hooks.ts": "82cb954fdbfa5a7555811eb0c8928c51446d556c6b0d38b3ac4fc464a6e59fa2",
});
const INSTALL_TRANSACTION_VERSION = 4;
const INSTALL_TRANSACTION_OWNER_VERSION = 1;
const INSTALL_TRANSACTION_COMMIT_PROOF_VERSION = 3;
const CODEX_TRUST_OBLIGATION_VERSION = 1;
const MANAGED_HOOK_SCRIPT_NAMES = Object.freeze([
  "guard-worktree.sh",
  "guard-worktree-write.py",
  "guard-protected-branch.sh",
  "show-phase-status.sh",
  "comment-checker.py",
]);
const MANAGED_HOOK_CONFIG_PATHS = Object.freeze([
  ".Codex/hooks.json",
  ".codex/hooks.json",
  ".claude/settings.json",
]);
const REQUIRED_MANAGED_HOST_FILES = Object.freeze([
  ".Codex/agents/code-reviewer.md",
  ".claude/agents/code-reviewer.md",
  ".omp/agents/code-reviewer.md",
  ".omp/extensions/agent-flow-hooks.ts",
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
  const activeHost = detectActiveHost();
  const agentFlowDir = path.join(root, ".agent-flow");
  recoverInterruptedCodexTrustTransaction(root);
  recoverStaleNodeInstallStartLock(root);
  const recoveryStartLock = acquireProjectStartLock(root, "node-install");
  try {
    recoverInterruptedInstallTransaction(root, agentFlowDir);
  } finally {
    releaseProjectStartLock(recoveryStartLock);
  }
  const nodeRuntimeRoot = path.join(agentFlowDir, "runtime", "node");
  assertSafeNodeRuntimeInstallTarget(root, nodeRuntimeRoot);
  const activeRun = findProjectActiveRun(root, { includeForeign: true });
  if (activeRun) {
    throw new Error(`install blocked while run ${activeRun.run_id ?? "unknown"} is active`);
  }
  const profile = detectProfile(root);
  let installSelection = resolveInstallSelection({ args: installArgs, detectedProfile: profile, kitRoot: KIT_ROOT, projectRoot: root });
  const existingPayload = readExistingKit(agentFlowDir);
  const legacyMigration = isSupportedLegacyMigrationPayload(existingPayload);
  const managedHookScripts = preflightManagedHookScripts(root, existingPayload, { legacyMigration });
  const managedHostFiles = preflightManagedHostFiles(
    root,
    authenticatedManagedHostPayload(existingPayload),
    { legacyMigration },
  );
  const previousSkillIndex = readExistingSkillIndex(agentFlowDir);
  const trustedPreviousSkillIndex = authenticatedPreviousSkillIndex(
    root,
    existingPayload,
    previousSkillIndex,
    installSelection,
    { legacyMigration },
  );
  installSelection = mergeInstallSelectionWithPrevious(installSelection, trustedPreviousSkillIndex, KIT_ROOT, root);
  const primaryProfile = installPrimaryProfile(profile, installSelection, existingPayload);
  const discoveredAutomaticExternalSkills = discoverAutomaticExternalSkillNames({
    home: HOME,
    activeHost,
    env: process.env,
    previousIndex: trustedPreviousSkillIndex,
  });
  const automaticExternalSkillRoots = new Set(
    [...discoveredAutomaticExternalSkills]
      .filter((name) => !installSelection.skillNames?.has(name))
      .filter((name) => name !== "agent-flow" && !GENERATED_PROJECT_SKILL_NAMES.has(name)),
  );
  const sourceResolvedSkillNames = installSelection.skillNames
    ? new Set(
        [...installSelection.skillNames]
          .filter((name) => name !== "agent-flow" && !GENERATED_PROJECT_SKILL_NAMES.has(name)),
      )
    : existingInstallSkillSnapshotNames(agentFlowDir);
  for (const name of discoveredAutomaticExternalSkills) {
    if (name !== "agent-flow" && !GENERATED_PROJECT_SKILL_NAMES.has(name)) {
      sourceResolvedSkillNames.add(name);
    }
  }
  const skillPlan = resolveProfileSkillSources({
    skillNames: sourceResolvedSkillNames,
    kitRoot: KIT_ROOT,
    projectRoot: root,
    projectSkillsRoot: path.join(agentFlowDir, "skills"),
    home: HOME,
    activeHost,
    env: process.env,
    previousIndex: trustedPreviousSkillIndex,
    automaticSkillNames: automaticExternalSkillRoots,
  });
  installSelection = mergeResolvedSkillClosure(installSelection, skillPlan);
  if (skillPlan.missing.length > 0) {
    throw new Error(`missing required profile skills: ${skillPlan.missing.join(", ")}`);
  }
  preflightInstallSkillSources(root, agentFlowDir, installSelection);
  const legacyRootScriptCriticalPaths = managedLegacyRootScriptCriticalPaths(root);
  const phases = fullFeaturePhases();
  const installStartLock = acquireProjectStartLock(root, "node-install");
  let persistentInstall;
  try {
    persistentInstall = beginCriticalInstallTransaction(
      root,
      agentFlowDir,
      [
        ...managedHostFiles.entries.map((entry) => entry.relative),
        ...legacyRootScriptCriticalPaths,
      ],
    );
    pendingProjectSkillHostTransaction = {
      root,
      hostTransaction: null,
      persistent: persistentInstall,
    };
    const racedActiveRun = findProjectActiveRun(root, { includeForeign: true });
    if (racedActiveRun) {
      throw new Error(`install blocked while run ${racedActiveRun.run_id ?? "unknown"} is active`);
    }
  } finally {
    releaseProjectStartLock(installStartLock);
  }

  for (const name of ["workflows", "skills", "templates", "prompts", "rules", "bootstrap"]) {
    ensureInstallDirectoryWithProgress(persistentInstall, path.join(agentFlowDir, name));
    if (name === "skills") {
      const skeletonHoldMs = Number.parseInt(
        process.env.AGENT_FLOW_TEST_HOLD_AFTER_DIRECTORY_SKELETON_MS ?? "0",
        10,
      );
      if (Number.isInteger(skeletonHoldMs) && skeletonHoldMs > 0 && skeletonHoldMs <= 10_000) {
        fs.writeFileSync(path.join(persistentInstall.transactionRoot, "open-journal-ready"), "ready\n", "utf8");
        Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, skeletonHoldMs);
      }
    }
  }

  ensureInstallDirectoryWithProgress(persistentInstall, path.join(agentFlowDir, "skills", "agent-flow"));
  ensureInstallDirectoryWithProgress(persistentInstall, path.join(agentFlowDir, "skills", "full-feature-workflow"));
  if (process.env.AGENT_FLOW_TEST_CRASH_AFTER_DIRECTORY_SKELETON === "1") {
    process.exit(85);
  }

  const payload = {
    install_scope: "project",
    profile,
    primary_profile: primaryProfile,
    profile_selection: installSelection.profileSelection,
    profiles: installSelection.profiles,
    selected_skills: installSelection.skillNames ? [...installSelection.skillNames].sort() : "all",
    root: ".",
    installed_at: existingPayload?.installed_at || new Date().toISOString(),
  };
  if (legacyMigration) {
    payload.migrated_from = {
      kind: "legacy-uncommitted-install",
      base_commit: LEGACY_MIGRATION_BASE_COMMIT,
    };
  }

  writeManagedFile(path.join(agentFlowDir, "workflows", "full-feature.yaml"), fullFeatureWorkflowYaml());
  copyBundledDirIfMissingOrSame(
    path.join(KIT_ROOT, "workflows"),
    path.join(agentFlowDir, "workflows"),
    true,
    new Set(),
    true,
    true,
  );
  const agentFlowSkill = agentFlowSkillMarkdown();
  writeManagedSkill(path.join(agentFlowDir, "skills", "agent-flow", "SKILL.md"), agentFlowSkill);
  writeManagedSkill(
    path.join(agentFlowDir, "skills", "full-feature-workflow", "SKILL.md"),
    fullFeatureSkillMarkdown(),
  );
  writeManagedSkill(path.join(agentFlowDir, "skills", "product-brief", "SKILL.md"), productBriefSkillMarkdown());
  writeManagedSkill(path.join(agentFlowDir, "skills", "plan-reviewer", "SKILL.md"), planReviewerSkillMarkdown());
  writeManagedSkill(
    path.join(agentFlowDir, "skills", "architecture-reviewer", "SKILL.md"),
    architectureReviewerSkillMarkdown(),
  );
  writeManagedSkill(path.join(agentFlowDir, "skills", "push-watch", "SKILL.md"), pushWatchSkillMarkdown());
  const snapshotCopySkips = new Set(
    skillPlan.entries
      .filter((entry) => entry.source_kind !== "bundled" || entry.replace_existing)
      .map((entry) => entry.name),
  );
  copyBundledDirIfMissingOrSame(
    path.join(KIT_ROOT, "skills"),
    path.join(agentFlowDir, "skills"),
    forceManaged,
    new Set(),
    true,
    forceManaged,
    new Set(["index.json", ...GENERATED_PROJECT_SKILL_NAMES, ...(installSelection.copyRootNames || [])]),
    installSelection.copyRootNames,
    snapshotCopySkips,
  );
  for (const entry of skillPlan.entries) {
    if (entry.source_kind === "bundled" && entry.replace_existing) {
      replaceSkillSnapshot(
        entry.source_path,
        path.join(agentFlowDir, "skills", entry.name),
        entry.tree_hash,
      );
    } else if (["shared", "host-bootstrap"].includes(entry.source_kind)) {
      copySkillSnapshot(
        entry.source_path,
        path.join(agentFlowDir, "skills", entry.name),
        entry.tree_hash,
      );
    }
  }
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, "profiles"), path.join(agentFlowDir, "profiles"), forceManaged);
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, "templates"), path.join(agentFlowDir, "templates"), forceManaged, new Set(), true, forceManaged);
  const skillIndex = installProjectSkills(root, agentFlowDir, trustedPreviousSkillIndex, forceManaged, installSelection, skillPlan);
  if (process.env.AGENT_FLOW_TEST_CRASH_AFTER_SKILL_INDEX === "1") {
    process.exit(84);
  }
  writeManagedFile(
    path.join(agentFlowDir, "skills", "upstream-lock.json"),
    fs.readFileSync(path.join(KIT_ROOT, "skills", "upstream-lock.json"), "utf8"),
  );
  writeManagedFile(
    path.join(agentFlowDir, "skills", "skill-consolidation.yaml"),
    fs.readFileSync(path.join(KIT_ROOT, "skills", "skill-consolidation.yaml"), "utf8"),
  );
  writeManagedFile(
    path.join(agentFlowDir, "skills", "source-policy.yaml"),
    fs.readFileSync(path.join(KIT_ROOT, "skills", "source-policy.yaml"), "utf8"),
  );
  applyManagedHookScriptPlan(managedHookScripts);
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, "scripts"), path.join(agentFlowDir, "scripts"), forceManaged);
  removeStaleContextDocsScripts(agentFlowDir, forceManaged);
  if (legacyRootScriptCriticalPaths.includes("scripts")) {
    removeManagedDirIfSame(path.join(KIT_ROOT, "scripts"), path.join(root, "scripts"));
  }
  copyBundledDirIfMissingOrSame(
    path.join(KIT_ROOT, "src", "agent_flow"),
    path.join(root, RUNTIME_PYTHON_RELATIVE, "agent_flow"),
    true,
    new Set(),
    true,
    true,
  );
  copyBundledDirIfMissingOrSame(
    path.join(KIT_ROOT, "templates", "_shared"),
    path.join(root, RUNTIME_PYTHON_RELATIVE, "agent_flow", "templates", "_shared"),
    true,
    new Set(),
    true,
    true,
  );
  copyBundledDirIfMissingOrSame(
    path.join(KIT_ROOT, "vendor", "python"),
    path.join(root, RUNTIME_PYTHON_RELATIVE),
    true,
    new Set(),
    true,
    true,
    new Set(["agent_flow"]),
  );
  ensureSafeNodeRuntimeDirectory(root, path.join(nodeRuntimeRoot, "bin"));
  writeSafeNodeRuntimeFile(
    root,
    path.join(nodeRuntimeRoot, "bin", "agent-flow-kit.mjs"),
    fs.readFileSync(path.join(KIT_ROOT, "bin", "agent-flow-kit.mjs")),
  );
  ensureSafeNodeRuntimeDirectory(root, path.join(nodeRuntimeRoot, "lib"));
  copyBundledDirIfMissingOrSame(
    path.join(KIT_ROOT, "lib"),
    path.join(nodeRuntimeRoot, "lib"),
    true,
    new Set(),
    true,
    true,
  );
  ensureSafeNodeRuntimeDirectory(root, path.join(nodeRuntimeRoot, "workflows"));
  copyBundledDirIfMissingOrSame(
    path.join(KIT_ROOT, "workflows"),
    path.join(nodeRuntimeRoot, "workflows"),
    true,
    new Set(),
    true,
    true,
  );
  ensureSafeNodeRuntimeDirectory(root, path.join(nodeRuntimeRoot, "profiles"));
  copyBundledDirIfMissingOrSame(
    path.join(KIT_ROOT, "profiles"),
    path.join(nodeRuntimeRoot, "profiles"),
    true,
    new Set(),
    true,
    true,
  );
  const projectLauncher = path.join(agentFlowDir, "bin", "agent-flow");
  const projectLauncherBytes = Buffer.from(projectLauncherSource(), "utf8");
  writeManagedFile(projectLauncher, projectLauncherBytes);
  if (process.platform !== "win32") chmodCriticalInstallFile(projectLauncher, 0o755);
  const projectRuntime = buildProjectRuntimeContract({
    launcherBytes: projectLauncherBytes,
    nodeRuntimeRoot,
    pythonRuntimeRoot: path.join(root, RUNTIME_PYTHON_RELATIVE),
  });
  payload.node_runtime = {
    path: NODE_RUNTIME_ENTRYPOINT_RELATIVE,
    tree_hash: projectRuntime.contract.node_runtime.tree_hash,
  };
  payload.python_runtime = {
    path: PYTHON_RUNTIME_ROOT_RELATIVE,
    tree_hash: projectRuntime.contract.python_runtime.tree_hash,
  };
  payload.project_runtime_contract = projectRuntime.contract;
  payload.project_runtime_contract_commitment_version = PROJECT_RUNTIME_CONTRACT_COMMITMENT_VERSION;
  payload.project_runtime_contract_commitment = projectRuntime.commitment;
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, ".Codex", "rules", "context"), path.join(root, ".Codex", "rules", "context"), forceManaged);
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, ".Codex", "context"), path.join(root, ".Codex", "context"), forceManaged);
  installCodexHooks(root);
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
  const promptProfileBlocks = installedProfilePromptBlocks(root, {
    profileIds: installSelection.profiles?.length ? installSelection.profiles : [primaryProfile],
    primaryProfile,
  });
  for (const phase of phases) {
    writeManagedFile(
      path.join(agentFlowDir, "prompts", `${phase.id}.md`),
      phasePrompt(phase, root, profilePromptBlockForPhase(promptProfileBlocks, phase.id)),
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
    ".agents/",
    ".claude/",
    ".omp/",
    "AGENTS.md",
    "CLAUDE.md",
    "AGENTS/",
    "CLAUDE/",
    "agent-flow/",
  ]);
  if (forceManaged) {
    removeGitignoreEntries(gitignorePath, ["scripts/check-context-docs.*"]);
  }
  upsertBootstrapBlock(path.join(root, "AGENTS.md"), "AGENTS.md");
  upsertBootstrapBlock(path.join(root, "CLAUDE.md"), "CLAUDE.md");
  makeHooksExecutable(root, managedHookScripts);
  installClaudeHooks(root);

  payload.skill_index = {
    path: ".agent-flow/skills/index.json",
    skills: skillIndex.skills.length,
    conflicts: skillIndex.conflicts.length,
    warnings: skillIndex.warnings.length,
  };
  payload.skill_upstream_lock = {
    path: "skills/upstream-lock.json",
    commit: JSON.parse(fs.readFileSync(path.join(KIT_ROOT, "skills", "upstream-lock.json"), "utf8")).commit,
    whole_tree_required: true,
    runtime_fetch: false,
  };
  payload.skill_source_policy = {
    path: "skills/source-policy.yaml",
    index_version: 2,
    hash_scope: "whole-tree",
  };
  payload.skill_plan_hash_version = 2;
  payload.skill_plan_hash = computeSkillPlanHash(skillIndex, root, true);
  payload.skill_links_commitment_version = SKILL_LINKS_COMMITMENT_VERSION;
  payload.skill_links_commitment = skillLinksCommitment(
    payload.skill_plan_hash,
    skillIndex.links,
  );
  installHostReviewers(managedHostFiles);
  installOmpHooks(root, managedHostFiles);
  assertManagedHostPlanApplied(managedHostFiles);
  payload.managed_host_files = managedHostFiles.manifest;
  payload.managed_host_files_commitment_version = MANAGED_HOST_FILES_COMMITMENT_VERSION;
  payload.managed_host_files_commitment = managedHostFilesCommitment(
    payload.skill_plan_hash,
    payload.managed_host_files,
  );
  payload.managed_hook_contract = buildManagedHookContract(root);
  payload.managed_hook_contract_commitment_version = MANAGED_HOOK_CONTRACT_COMMITMENT_VERSION;
  payload.managed_hook_contract_commitment = managedHookContractCommitment(
    payload.skill_plan_hash,
    payload.managed_hook_contract,
  );
  if (process.env.AGENT_FLOW_TEST_CRASH_AFTER_MANAGED_HOST_APPLY === "1") {
    process.exit(86);
  }
  const failureHoldMs = Number.parseInt(
    process.env.AGENT_FLOW_TEST_HOLD_BEFORE_LATE_INSTALL_FAILURE_MS ?? "0",
    10,
  );
  if (Number.isInteger(failureHoldMs) && failureHoldMs > 0 && failureHoldMs <= 10_000) {
    fs.writeFileSync(path.join(persistentInstall.transactionRoot, "late-failure-ready"), "ready\n", "utf8");
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, failureHoldMs);
  }
  if (process.env.AGENT_FLOW_TEST_FAIL_AFTER_MANAGED_HOST_APPLY === "1") {
    throw new Error("injected install failure after managed host apply");
  }

  writeJson(path.join(agentFlowDir, "kit.json"), payload);
  const activeCodexTrustPlan = activeHost === "codex"
    ? prepareCodexTrustState(root, { required: true })
    : null;
  if (activeCodexTrustPlan) {
    registerCodexTrustObligation(persistentInstall, activeCodexTrustPlan);
  }
  commitPendingProjectSkillHostTransaction({
    afterCommit: activeHost === "codex"
      ? () => applyCodexTrustState(root, activeCodexTrustPlan, { required: true })
      : null,
  });
  for (const name of ["runs", "state", "handoffs", "team", "worktrees", "local-skills"]) {
    fs.mkdirSync(path.join(agentFlowDir, name), { recursive: true });
  }
  if (activeHost !== "codex") {
    try {
      applyCodexTrustState(root, prepareCodexTrustState(root));
    } catch (error) {
      console.error(`warning: Codex trust registration failed after project commit: ${error instanceof Error ? error.message : String(error)}`);
    }
  }
  console.log(`agent-flow installed profile=${profile}`);
  console.log(`next_command: ${AGENT_FLOW_COMMAND} status`);
}

function existingInstallSkillSnapshotNames(agentFlowDir) {
  const names = new Set();
  const snapshotRoot = path.join(agentFlowDir, "skills");
  const metadata = lstatIfExists(snapshotRoot);
  if (!metadata) return names;
  if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
    throw new Error(`blocked: skill snapshot root must be a regular directory: ${snapshotRoot}`);
  }
  for (const entry of fs.readdirSync(snapshotRoot, { withFileTypes: true })) {
    if (!entry.isDirectory() && !entry.isSymbolicLink()) continue;
    if (entry.name === "agent-flow" || GENERATED_PROJECT_SKILL_NAMES.has(entry.name)) continue;
    names.add(entry.name);
  }
  return names;
}

function assertSafeNodeRuntimeInstallTarget(root, runtimeRoot) {
  const relative = path.relative(path.resolve(root), path.resolve(runtimeRoot));
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(`pinned Node runtime install target escapes the project: ${runtimeRoot}`);
  }
  let cursor = path.resolve(root);
  for (const part of relative.split(path.sep).filter(Boolean)) {
    cursor = path.join(cursor, part);
    const metadata = lstatIfExists(cursor);
    if (!metadata) return;
    if (metadata.isSymbolicLink()) {
      throw new Error(`pinned Node runtime install target uses a symlink: ${cursor}`);
    }
    if (!metadata.isDirectory()) {
      throw new Error(`pinned Node runtime install target has a non-directory ancestor: ${cursor}`);
    }
  }
  assertSafeNodeRuntimeTree(runtimeRoot);
}

function assertSafeNodeRuntimeTree(directory) {
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const candidate = path.join(directory, entry.name);
    if (entry.isSymbolicLink()) {
      throw new Error(`pinned Node runtime install target uses a symlink: ${candidate}`);
    }
    if (entry.isDirectory()) {
      assertSafeNodeRuntimeTree(candidate);
    } else if (!entry.isFile()) {
      throw new Error(`pinned Node runtime install target contains a special file: ${candidate}`);
    }
  }
}

function ensureSafeNodeRuntimeDirectory(root, directory) {
  const relative = path.relative(path.resolve(root), path.resolve(directory));
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(`pinned Node runtime directory escapes the project: ${directory}`);
  }
  const installTransaction = pendingProjectSkillHostTransaction?.persistent;
  if (installTransaction) {
    ensureInstallDirectoryWithProgress(installTransaction, directory);
    return;
  }
  let cursor = path.resolve(root);
  for (const part of relative.split(path.sep).filter(Boolean)) {
    cursor = path.join(cursor, part);
    const metadata = lstatIfExists(cursor);
    if (!metadata) {
      fs.mkdirSync(cursor);
    } else if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
      throw new Error(`unsafe pinned Node runtime directory: ${cursor}`);
    }
  }
}

function writeSafeNodeRuntimeFile(root, destination, content) {
  ensureSafeNodeRuntimeDirectory(root, path.dirname(destination));
  const metadata = lstatIfExists(destination);
  if (metadata && (metadata.isSymbolicLink() || !metadata.isFile())) {
    throw new Error(`unsafe pinned Node runtime file: ${destination}`);
  }
  atomicInstallWrite(destination, content);
}

function runWorkflowCommand(args) {
  let subcommand = args[0];
  // One public command contract: legacy user-facing forms are normalized
  // before reaching the Node transition writer.
  if (subcommand === undefined || subcommand === "status") {
    subcommand = "status";
    args = subcommand === "status" && args[0] === "status" ? ["status", ...args.slice(1)] : ["status"];
  } else if (!["start", "status", "next", "advance", "push-watch", "push-watch-tick"].includes(subcommand)) {
    args = ["start", "--task", args.join(" ")];
    subcommand = "start";
  }
  const approvePause = args.includes("--approve-pause");
  if (approvePause && subcommand !== "advance") {
    throw new Error("blocked: --approve-pause is only valid for run advance");
  }
  const root = resolveAgentFlowRoot(process.cwd());
  assertNoOpenInstallTransaction(root);
  if (subcommand === "start") {
    const task = optionValue(args, "--task");
    if (!task) {
      throw new Error("run start requires --task");
    }
    const workflow = optionValue(args, "--workflow") ?? "full-feature";
    const runId = optionValue(args, "--run-id") ?? newRunId();
    const requestedWorktreeName = optionValue(args, "--worktree");
    const requestedWorktreeBranch = optionValue(args, "--worktree-branch");
    const rawLedgerMode = process.env.AGENT_FLOW_LEDGER_MODE;
    const experimentEnabled = typeof rawLedgerMode === "string" && rawLedgerMode.trim().length > 0;
    const ledgerMode = resolveLedgerMode(rawLedgerMode);
    assertInstalled(root);
    const naming = configuredWorktreeNaming(root);
    assertStartWorkspaceSupported(root, naming);
    const startLock = acquireProjectStartLock(root, "node");
    let runDir = null;
    let workspace = null;
    let runDirCreated = false;
    let committed = false;
    try {
      assertNoOpenInstallTransaction(root);
      const active = findProjectActiveRun(root, { includeForeign: true });
      if (active) {
        throw new Error(
          `blocked: active run ${active.run_id ?? "unknown"} is pinned to ${active.workspace_root ?? root}; ` +
          "continue it from the same worktree instead of starting a new run",
        );
      }
      const phases = workflowPhases(workflow);
      const startedAt = new Date().toISOString();
      const loreSnapshot = createLoreSnapshot(root, task);
      const skillPlanHash = currentSkillPlanHash(root);
      const localSkillPlanHash = projectLocalSkillPlanHash(root);
      const requestedWorkspace = resolveGitTopLevel(process.cwd()) ?? process.cwd();
      workspace = prepareRunWorkspace(root, requestedWorkspace, task, naming, {
        name: requestedWorktreeName,
        branch: requestedWorktreeBranch,
      });
      const stateRoot = nodeStateRootForWorkspace(root, workspace.path);
      runDir = path.join(stateRoot, ".agent-flow", "runs", workflow, runId);
      if (fs.existsSync(runDir)) throw new Error(`run already exists: ${runId}`);
      const baseSnapshot = configuredRunBase(root, workspace.path);
      fs.mkdirSync(path.join(runDir, "artifacts"), { recursive: true });
      fs.mkdirSync(path.join(runDir, "logs"), { recursive: true });
      runDirCreated = true;
      const state = {
        run_id: runId,
        workflow,
        task,
        phase_index: 0,
        phase: phases[0].id,
        status: "running",
        run_dir: runDir,
        started_at: startedAt,
        phase_entered_at: startedAt,
        workspace_root: workspace.path,
        worktree_mode: naming.worktree,
        base_ref: baseSnapshot.base_ref,
        base_commit: baseSnapshot.base_commit,
        skill_plan_hash: skillPlanHash,
        skill_plan_hash_version: 2,
        local_skill_plan_hash: localSkillPlanHash,
        local_skill_plan_hash_version: LOCAL_SKILL_PLAN_HASH_VERSION,
        ledger_mode: ledgerMode,
        experiment_enabled: experimentEnabled,
        runner_workflow_hash: runnerWorkflowHash(workflow, phases),
        runner_workflow_hash_version: 1,
        ...(experimentEnabled ? { phase_revision: 0 } : {}),
        ...loreSnapshot,
      };
      assertNoOpenInstallTransaction(root);
      const initializedLedger = initializeExecutionStateLedger({
        runDir,
        runId,
        mode: ledgerMode,
        experimentEnabled,
        task,
        workflowId: workflow,
        workflowPhases: phases,
        baseCommit: baseSnapshot.base_commit,
        experiment: ledgerExperimentControlsFromEnvironment(),
        runSnapshot: ledgerObservedRunSnapshot(root, state, phases),
      });
      if (experimentEnabled && initializedLedger.ok !== true) {
        throw new Error(`execution ledger pilot initialization failed: ${initializedLedger.error ?? "unknown error"}`);
      }
      const observation = observeExecutionStateInjection({
        runDir,
        runId,
        mode: ledgerMode,
        experimentEnabled,
        phase: phases[0],
        projectRoot: workspaceRootForState(state, root),
        round: ledgerPromptRound(state),
        generatedAt: startedAt,
        promptBytes: Buffer.byteLength(String(phases[0].instruction ?? ""), "utf8"),
      });
      const ledgerBlock = committedObservationBlock(observation, experimentEnabled);
      writeJson(path.join(runDir, "manifest.json"), state);
      writeJson(currentRunPath(root, state), state);
      committed = true;
      printNext(state, root, {
        ledgerBlock,
        phases,
      });
      return;
    } catch (error) {
      if (!committed && runDirCreated && runDir) fs.rmSync(runDir, { recursive: true, force: true });
      if (!committed && workspace?.created) {
        const cleanupError = cleanupCreatedRunWorkspace(root, workspace);
        if (cleanupError) {
          throw new Error(`${error instanceof Error ? error.message : String(error)}; cleanup failed: ${cleanupError}`);
        }
      }
      throw error;
    } finally {
      releaseProjectStartLock(startLock);
    }
  }

  if (subcommand === "status") {
    const discovered = findProjectActiveRun(root, {
      includeCompletedPointer: true,
      includeForeign: true,
    });
    if (discovered?.runtime === "python") {
      relayPythonStatus(root, { hookFormat: args.includes("--format") && optionValue(args, "--format") === "hook" });
      return;
    }
    if (!discovered) {
      throw new Error(`no active run. start one with: ${AGENT_FLOW_COMMAND} run "<task>"`);
    }
    const state = assertLoreSnapshotPinned(assertWorkspacePinned(discovered), root);
    assertSkillPlanPinned(state, root);
    const phases = verifiedRunnerWorkflowPhases(state);
    if (args.includes("--format") && optionValue(args, "--format") === "hook") {
      console.log(JSON.stringify(computeStatusPayload(state, root, phases)));
      return;
    }
    printStatus(state, root, phases);
    return;
  }

  if (subcommand === "next") {
    const state = assertLoreSnapshotPinned(assertWorkspacePinned(readCurrentRun(root)), root);
    assertSkillPlanPinned(state, root);
    const phases = verifiedRunnerWorkflowPhases(state);
    holdAfterVerifiedWorkflowLoad(root, state);
    const recovered = recoverPendingNodeTransition(root, state, phases);
    if (recovered) {
      printNodeTransitionResult(recovered, root);
      return;
    }
    const phase = phases[state.phase_index];
    if (state.status !== "complete" && state.phase !== "complete" && phase) {
      const observation = observeExecutionStateInjection({
        runDir: resolveRunDir(root, state.run_dir),
        runId: state.run_id,
        mode: pinnedLedgerMode(state),
        experimentEnabled: state.experiment_enabled === true,
        phase,
        projectRoot: workspaceRootForState(state, root),
        round: ledgerPromptRound(state),
        generatedAt: new Date().toISOString(),
        promptBytes: Buffer.byteLength(String(phase.instruction ?? ""), "utf8"),
      });
      printNext(state, root, {
        ledgerBlock: committedObservationBlock(observation, state.experiment_enabled === true),
        phases,
      });
      return;
    }
    printNext(state, root, { ledgerBlock: "", phases });
    return;
  }

  if (subcommand === "push-watch") {
    assertInstalled(root);
    const branch = currentBranch(process.cwd());
    if (["main", "master", "develop"].includes(branch)) {
      throw new Error(`blocked: protected branch ${branch}`);
    }
    const activeState = assertLoreSnapshotPinned(assertWorkspacePinned(readCurrentRun(root)), root);
    assertSkillPlanPinned(activeState, root);
    const state = {
      status: "watching",
      branch,
      iterations: 0,
      updated_at: new Date().toISOString(),
    };
    writeJson(pushWatchStatePath(root, activeState), state);
    console.log(`push-watch watching branch=${branch}`);
    return;
  }

  if (subcommand === "push-watch-tick") {
    assertInstalled(root);
    const state = assertLoreSnapshotPinned(assertWorkspacePinned(readCurrentRun(root)), root);
    assertSkillPlanPinned(state, root);
    if (state.phase !== "pr-watch") {
      throw new Error(`blocked: push-watch-tick requires current phase pr-watch, got ${state.phase}`);
    }
    const runDir = resolveRunDir(root, state.run_dir);
    const pr = readPullRequestStatus(workspaceRootForState(state, root));
    const watchStatus = pullRequestWatchStatus(pr);
    const artifact = path.join(runDir, "artifacts", "pr-watch.md");
    writeManagedFile(
      artifact,
      [`status: ${watchStatus}`, `pr: ${pr.url ?? "unknown"}`, `recorded_at: ${new Date().toISOString()}`, ""].join("\n"),
    );
    const watchStatePath = pushWatchStatePath(root, state);
    const previous = fs.existsSync(watchStatePath)
      ? JSON.parse(fs.readFileSync(watchStatePath, "utf8"))
      : {};
    writeJson(watchStatePath, {
      ...previous,
      status: watchStatus,
      pr: pr.url ?? null,
      iterations: Number(previous.iterations ?? 0) + 1,
      updated_at: new Date().toISOString(),
    });
    console.log(`push-watch status=${watchStatus}`);
    return;
  }

  if (subcommand === "advance") {
    const state = assertLoreSnapshotPinned(assertWorkspacePinned(readCurrentRun(root)), root);
    assertSkillPlanPinned(state, root);
    const runDir = resolveRunDir(root, state.run_dir);
    if (state.status === "complete" || state.phase === "complete") {
      if (approvePause) {
        throw new Error("blocked: --approve-pause requires an active pause request");
      }
      console.log(`workflow already complete: ${state.run_id}`);
      return;
    }
    const phases = verifiedRunnerWorkflowPhases(state);
    holdAfterVerifiedWorkflowLoad(root, state);
    const recovered = recoverPendingNodeTransition(root, state, phases);
    if (recovered) {
      printNodeTransitionResult(recovered, root);
      return;
    }
    const phase = phases[state.phase_index];
    if (approvePause) {
      if (!phase?.pause_after) {
        throw new Error(`blocked: --approve-pause is invalid for non-pause phase ${phase?.id ?? "complete"}`);
      }
      if (!state.pause_after_pending || state.pause_after_pending.phase !== phase.id) {
        throw new Error(`blocked: --approve-pause requires an existing pause request for ${phase.id}`);
      }
    }
    const artifact = path.join(runDir, phase.artifact);
    if (!fs.existsSync(artifact)) {
      throw new Error(`blocked: missing artifact ${artifact}`);
    }
    assertFreshArtifact(state, phase, artifact);
    assertCompletionMarkers(phase, artifact, root, localSkillContextForState(state, root));
    let transitionState = state;
    if (phase.pause_after && !pauseAfterApprovalMatches(state, phase, artifact)) {
      if (approvePause && pauseAfterPendingMatches(state, phase, artifact)) {
        transitionState = {
          ...state,
          pause_after_approval: {
            phase: phase.id,
            artifact_sha256: artifactSha256(artifact),
            approved_at: new Date().toISOString(),
          },
        };
        writeJson(path.join(runDir, "manifest.json"), transitionState);
        writeJson(currentRunPath(root, transitionState), transitionState);
      } else {
        const pendingMatches = pauseAfterPendingMatches(state, phase, artifact);
        const pausedState = {
          ...state,
          status: "blocked",
          updated_at: new Date().toISOString(),
          pause_after_pending: {
            phase: phase.id,
            artifact_sha256: artifactSha256(artifact),
            requested_at: pendingMatches && typeof state.pause_after_pending?.requested_at === "string"
              ? state.pause_after_pending.requested_at
              : new Date().toISOString(),
          },
        };
        if (!pendingMatches) {
          delete pausedState.pause_after_approval;
        }
        writeJson(path.join(runDir, "manifest.json"), pausedState);
        writeJson(currentRunPath(root, pausedState), pausedState);
        printPauseAfter(pausedState, phase, root);
        return;
      }
    }
    const routeKey = phase.routes ? nodeRouteKey(phase, artifact) : "sequential";
    const nextIndex = nextPhaseIndex(transitionState, phases, phase, artifact, routeKey);
    const nextPhase = phases[nextIndex];
    const fixLoopRounds = nextFixLoopRounds(transitionState, phase, nextPhase);
    const transitionedAt = new Date().toISOString();
    const nextState = {
      ...transitionState,
      phase_index: nextIndex,
      phase: nextPhase?.id ?? "complete",
      status: nextPhase ? "running" : "complete",
      updated_at: transitionedAt,
      phase_entered_at: transitionedAt,
      fix_loop_rounds: fixLoopRounds,
    };
    if (nextState.experiment_enabled === true) {
      nextState.phase_revision = phaseRevision(transitionState) + 1;
    }
    delete nextState.pause_after_pending;
    if (nextState.experiment_enabled === true) {
      writePendingNodeTransition(runDir, createPendingNodeTransition({
        state: transitionState,
        nextState,
        nextPhase,
        currentIndex: transitionState.phase_index,
        nextIndex,
        transitionedAt,
        routeKey,
        fixLoopRounds,
      }));
      const recovered = recoverPendingNodeTransition(root, transitionState, phases);
      if (!recovered) throw new Error("execution ledger pending transition was not recoverable");
      printNodeTransitionResult(recovered, root);
      return;
    }
    syncRouteArtifacts(runDir, phases, transitionState.phase_index, nextIndex);
    writeJson(path.join(runDir, "manifest.json"), nextState);
    writeJson(currentRunPath(root, nextState), nextState);
    printNodeTransitionResult({ state: nextState, phases, ledgerBlock: "" }, root);
    return;
  }

  throw new Error("usage: agent-flow-kit run <install|start|status|next|advance|push-watch|push-watch-tick>");
}

function loadWorkflowDefinition(name) {
  if (!/^[A-Za-z0-9_-]+$/.test(name)) {
    throw new Error(`unsafe workflow name: ${name}`);
  }
  const workflowPath = path.join(KIT_ROOT, "workflows", `${name}.yaml`);
  const text = fs.readFileSync(workflowPath, "utf8");
  const definition = parseWorkflowYaml(text, name, workflowPath);
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
    pause_after: normalizeExportedBoolean(phase.pause_after, name, index, "pause_after"),
    optional: normalizeExportedBoolean(phase.optional, name, index, "optional"),
    multi_review: normalizeExportedBoolean(phase.multi_review, name, index, "multi_review"),
    cite_lore: normalizeExportedBoolean(phase.cite_lore, name, index, "cite_lore"),
    routes: normalizeExportedRoutes(phase.routes, name, index),
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
  if (hasSpringMarkers(rootDir)) {
    return "spring";
  }
  if (PYTHON_PROJECT_MARKERS.some((name) => fs.existsSync(path.join(rootDir, name)))) {
    return "python";
  }
  const earlyPackagePath = path.join(rootDir, "package.json");
  let packageText = null;
  let dependencyNames = new Set();
  if (fs.existsSync(earlyPackagePath)) {
    packageText = fs.readFileSync(earlyPackagePath, "utf8");
    dependencyNames = packageDependencyNames(packageText);
    if (dependencyNames.has("react-native") || dependencyNames.has("expo")) {
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
    packageText ??= fs.readFileSync(packagePath, "utf8");
    if (dependencyNames.has("next")) {
      return "nextjs";
    }
    if (dependencyNames.has("react")) {
      return "react";
    }
    if (hasNodeBackendMarkers(packageText)) {
      return "node";
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

function hasNodeBackendMarkers(packageText) {
  const dependencyNames = packageDependencyNames(packageText);
  return ["express", "@nestjs/core", "fastify", "koa", "@hapi/hapi", "hapi"]
    .some((name) => dependencyNames.has(name));
}

function packageDependencyNames(packageText) {
  let payload;
  try {
    payload = JSON.parse(packageText);
  } catch {
    return new Set();
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return new Set();
  }
  return new Set(
    ["dependencies", "devDependencies", "peerDependencies", "optionalDependencies"]
      .flatMap((section) => (
        payload[section] && typeof payload[section] === "object" && !Array.isArray(payload[section])
          ? Object.keys(payload[section])
          : []
      )),
  );
}

function hasSpringMarkers(rootDir) {
  const markers = ["org.springframework", "spring-boot"];
  const relatives = [
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    path.join("gradle", "libs.versions.toml"),
  ];
  for (const entry of fs.readdirSync(rootDir, { withFileTypes: true })) {
    if (!entry.isDirectory() || entry.name.startsWith(".") || entry.name === "node_modules") continue;
    relatives.push(path.join(entry.name, "pom.xml"));
    relatives.push(path.join(entry.name, "build.gradle"));
    relatives.push(path.join(entry.name, "build.gradle.kts"));
  }
  for (const relative of relatives) {
    const buildFile = path.join(rootDir, relative);
    if (!fs.existsSync(buildFile)) continue;
    const text = fs.readFileSync(buildFile, "utf8").toLowerCase();
    if (markers.some((marker) => text.includes(marker))) return true;
  }
  return false;
}

function hasChildWithSuffix(rootDir, suffix) {
  if (!fs.existsSync(rootDir)) {
    return false;
  }
  return fs.readdirSync(rootDir).some((name) => name.endsWith(suffix));
}

function resolveAgentFlowRoot(start) {
  const worktreeRoot = resolveManagedWorktreeRoot(start);
  if (worktreeRoot && fs.existsSync(path.join(worktreeRoot, ".agent-flow", "kit.json"))) {
    return worktreeRoot;
  }
  const gitCommonRoot = resolveGitCommonWorktreeRoot(start);
  if (gitCommonRoot && fs.existsSync(path.join(gitCommonRoot, ".agent-flow", "kit.json"))) {
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
  const parts = start.split(path.sep);
  const markers = new Set([".agent-flow", ".codex", ".Codex", ".claude", ".omp"]);
  for (let index = parts.length - 2; index >= 0; index -= 1) {
    if (parts[index + 1] !== "worktrees") continue;
    if (!markers.has(parts[index])) continue;
    const root = parts.slice(0, index).join(path.sep) || path.sep;
    // 홈의 전역 Codex/OMP worktree는 프로젝트 내부 worktree가 아니다.
    if (HOME && samePath(root, HOME) && [".codex", ".Codex", ".claude", ".omp"].includes(parts[index])) {
      continue;
    }
    return root;
  }
  return null;
}

function samePath(left, right) {
  try {
    return fs.realpathSync.native(left) === fs.realpathSync.native(right);
  } catch {
    // 심볼릭 링크가 섞인 임시 경로에서도 홈 비교는 보수적으로 처리한다.
    return path.resolve(left) === path.resolve(right);
  }
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
  const result = safeSpawnSync("git", args, {
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

function safeSpawnSync(commandName, args, options = {}) {
  // 외부 CLI는 자동 relay를 멈추지 않도록 기본 timeout을 둔다.
  return spawnSync(commandName, args, {
    timeout: options.timeout ?? 30_000,
    ...options,
  });
}

function readCurrentRun(root) {
  const recovered = findProjectActiveRun(root, { includeCompletedPointer: true });
  if (!recovered) {
    throw new Error(`no active run. start one with: ${AGENT_FLOW_COMMAND} run "<task>"`);
  }
  const state = normalizeRunState(root, recovered);
  if (state.workspace_root == null && !["complete", "aborted"].includes(state.status)) {
    if (configuredWorktreeNaming(root).worktree !== "disabled") {
      throw new Error("blocked: active run is missing its required registered worktree workspace_root");
    }
    const migrated = { ...state, workspace_root: path.resolve(root) };
    writeJson(path.join(resolveRunDir(root, state.run_dir), "manifest.json"), migrated);
    writeJson(currentRunPath(root, migrated), migrated);
    return migrated;
  }
  return state;
}

function relayPythonStatus(root, { hookFormat = false } = {}) {
  const pythonPathEntries = [
    installedPythonRuntimePath(root),
    path.join(KIT_ROOT, "src"),
    process.env.PYTHONPATH,
  ].filter(Boolean);
  const result = safeSpawnSync(
    preferredPython(),
    ["-m", "agent_flow.cli", "status", "--root", root],
    {
      cwd: root,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
      env: {
        ...process.env,
        AGENT_FLOW_STATUS_RELAY: "node-to-python",
        PYTHONPATH: [...new Set(pythonPathEntries)].join(path.delimiter),
      },
      timeout: 30_000,
    },
  );
  if (result.error || result.status !== 0) {
    const detail = result.error?.message || result.stderr?.trim() || result.stdout?.trim() || `exit ${result.status}`;
    throw new Error(`Python status relay failed: ${detail}`);
  }
  if (hookFormat) {
    const statusLine = result.stdout.split(/\r?\n/).find((line) => line.startsWith("status_json: "));
    if (!statusLine) throw new Error("Python status relay did not emit status_json");
    const payload = JSON.parse(statusLine.slice("status_json: ".length));
    console.log(JSON.stringify(payload));
    return;
  }
  process.stdout.write(result.stdout.endsWith("\n") ? result.stdout : `${result.stdout}\n`);
}

function findProjectActiveRun(root, { includeCompletedPointer = false, includeForeign = false } = {}) {
  const pointers = readNodeRunPointers(root);
  const manifests = scanRunManifests(root);
  const activeManifests = manifests.filter(
    (state) => !["complete", "aborted"].includes(state.status) && state.phase !== "complete",
  );
  if (activeManifests.length > 1) {
    throw new Error(
      `blocked: multiple active run manifests found: ${activeManifests.map((state) => state.run_id).join(", ")}`,
    );
  }
  const active = activeManifests[0] ?? null;
  const activePointers = pointers.filter(
    ({ state }) => !["complete", "aborted"].includes(state.status) && state.phase !== "complete",
  );
  if (activePointers.length > 1) {
    throw new Error("blocked: multiple active current run pointers found");
  }
  const activePointer = activePointers[0]?.state ?? null;
  const foreign = includeForeign ? findPythonActiveRun(root) : null;
  if (active && foreign) {
    throw new Error(`blocked: multiple cross-runtime active runs found: ${active.run_id}, ${foreign.run_id}`);
  }
  if (activePointer) {
    const matchingManifest = manifests.find((state) => sameRunIdentity(activePointer, state));
    if (
      (!active || stableControlSha256(activePointer) !== stableControlSha256(active))
      && pendingNodeTransitionMatchesStates(root, activePointer, matchingManifest)
    ) {
      return normalizeRunState(root, activePointer);
    }
    if (
      !active
      || !sameRunIdentity(activePointer, active)
      || stableControlSha256(activePointer) !== stableControlSha256(active)
    ) {
      throw new Error("blocked: current run pointer does not match an active run manifest");
    }
  }
  if (active) {
    const normalized = normalizeRunState(root, active);
    writeJson(currentRunPath(root, normalized), normalized);
    return normalized;
  }
  if (foreign) return foreign;
  if (includeCompletedPointer && pointers.length > 0) {
    const pointer = [...pointers].sort((left, right) => right.mtimeMs - left.mtimeMs)[0].state;
    const matching = manifests.find((state) => sameRunIdentity(pointer, state));
    if (!matching) {
      throw new Error("blocked: current run pointer does not match a run manifest");
    }
    return normalizeRunState(root, matching);
  }
  return null;
}

function pendingNodeTransitionMatchesStates(root, fromState, nextState) {
  if (!nextState || !sameRunIdentity(fromState, nextState)) return false;
  try {
    const journalPath = pendingNodeTransitionPath(resolveRunDir(root, nextState.run_dir));
    if (!fs.existsSync(journalPath)) return false;
    const pending = readStrictJsonObject(journalPath, "execution ledger pending transition");
    const { content_sha256: _contentSha256, ...base } = pending;
    return pending.schema_version === PENDING_NODE_TRANSITION_VERSION
      && pending.runtime === "node"
      && pending.run_id === fromState.run_id
      && pending.workflow_id === fromState.workflow
      && pending.content_sha256 === stableControlSha256(base)
      && pending.from_state_sha256 === stableControlSha256(fromState)
      && pending.next_state_sha256 === stableControlSha256(nextState)
      && pending.from_state_sha256 === stableControlSha256(pending.from_state)
      && pending.next_state_sha256 === stableControlSha256(pending.next_state);
  } catch (_error) {
    return false;
  }
}

function scanRunManifests(root) {
  const states = [];
  for (const stateRoot of nodeStateRoots(root)) {
    const runsRoot = path.join(stateRoot, ".agent-flow", "runs");
    if (!fs.existsSync(runsRoot)) continue;
    for (const workflowEntry of safeDirectoryEntries(runsRoot, `runs root ${stateRoot}`)) {
      if (!workflowEntry.isDirectory() || workflowEntry.isSymbolicLink()) continue;
      const workflowRoot = path.join(runsRoot, workflowEntry.name);
      for (const runEntry of safeDirectoryEntries(workflowRoot, `workflow run root ${workflowEntry.name}`)) {
        if (!runEntry.isDirectory() || runEntry.isSymbolicLink()) continue;
        const manifestPath = path.join(workflowRoot, runEntry.name, "manifest.json");
        if (!fs.existsSync(manifestPath)) continue;
        const raw = readStrictJsonObject(manifestPath, "run manifest");
        if (typeof raw.workflow_id === "string" && typeof raw.workflow !== "string") continue;
        const state = readRunStateFile(manifestPath, "run manifest");
        const expectedRunDir = path.join(stateRoot, ".agent-flow", "runs", workflowEntry.name, runEntry.name);
        if (
          state.workflow !== workflowEntry.name
          || state.run_id !== runEntry.name
          || path.resolve(stateRoot, state.run_dir) !== path.resolve(expectedRunDir)
        ) {
          throw new Error(`blocked: run manifest identity mismatch: ${manifestPath}`);
        }
        states.push({
          ...state,
          run_dir: path.resolve(stateRoot, state.run_dir),
        });
      }
    }
  }
  return states;
}

function readNodeRunPointers(root) {
  const pointers = [];
  for (const stateRoot of nodeStateRoots(root)) {
    const pointerPath = path.join(stateRoot, ".agent-flow", "state", "current-run.json");
    if (!fs.existsSync(pointerPath)) continue;
    const state = readRunStateFile(pointerPath, "current run pointer");
    pointers.push({
      state: {
        ...state,
        run_dir: path.resolve(stateRoot, state.run_dir),
      },
      mtimeMs: fs.lstatSync(pointerPath).mtimeMs,
    });
  }
  return pointers;
}

function nodeStateRoots(root) {
  const roots = [path.resolve(root)];
  const commonDir = gitOutput(root, ["rev-parse", "--git-common-dir"]);
  if (!commonDir) return roots;
  const registrationsRoot = path.join(path.resolve(root, commonDir), "agent-flow", "worktrees");
  const metadata = lstatIfExists(registrationsRoot);
  if (!metadata) return roots;
  if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
    throw new Error(`blocked: unsafe worktree registration root: ${registrationsRoot}`);
  }
  for (const entry of safeDirectoryEntries(registrationsRoot, "worktree registration root")) {
    if (entry.isDirectory() && !entry.isSymbolicLink()) roots.push(path.join(registrationsRoot, entry.name));
  }
  return roots;
}

function findPythonActiveRun(root) {
  const stateRoots = [path.resolve(root)];
  const commonDir = gitOutput(root, ["rev-parse", "--git-common-dir"]);
  if (commonDir) {
    const worktreesRoot = path.join(path.resolve(root, commonDir), "agent-flow", "worktrees");
    if (fs.existsSync(worktreesRoot)) {
      for (const entry of safeDirectoryEntries(worktreesRoot, "python worktree runtime root")) {
        if (entry.isSymbolicLink() || !entry.isDirectory()) continue;
        stateRoots.push(path.join(worktreesRoot, entry.name));
      }
    }
  }
  const active = [];
  for (const stateRoot of stateRoots) {
    collectPythonLegacyActiveRuns(stateRoot, root, active);
    if (!samePath(stateRoot, root)) collectPythonStateActiveRuns(stateRoot, root, active);
  }
  if (active.length > 1) {
    throw new Error(`blocked: multiple Python active runs found: ${active.map((state) => state.run_id).join(", ")}`);
  }
  return active[0] ?? null;
}

function collectPythonLegacyActiveRuns(stateRoot, root, active) {
  const runsRoot = path.join(stateRoot, ".agent-flow", "runs");
  if (!fs.existsSync(runsRoot)) return;
  for (const entry of safeDirectoryEntries(runsRoot, `Python runs root ${stateRoot}`)) {
    if (entry.isSymbolicLink() || !entry.isDirectory()) continue;
    const runRoot = path.join(runsRoot, entry.name);
    const marker = path.join(runRoot, "active");
    if (!fs.existsSync(marker)) continue;
    const markerStat = fs.lstatSync(marker);
    if (markerStat.isSymbolicLink() || !markerStat.isFile()) {
      throw new Error(`blocked: unsafe Python active marker: ${marker}`);
    }
    const metaPath = path.join(runRoot, "meta.json");
    const meta = readStrictJsonObject(metaPath, "Python run metadata");
    if (meta.run_id !== entry.name || typeof meta.workflow !== "string" || typeof meta.task !== "string") {
      throw new Error(`blocked: Python run metadata identity mismatch: ${metaPath}`);
    }
    active.push({
      run_id: meta.run_id,
      workflow: meta.workflow,
      task: meta.task,
      status: "running",
      runtime: "python",
      workspace_root: pythonWorkspaceForStateRoot(stateRoot, root),
    });
  }
}

function collectPythonStateActiveRuns(stateRoot, root, active) {
  const runsRoot = path.join(stateRoot, ".agent-flow", "runs");
  if (!fs.existsSync(runsRoot)) return;
  for (const workflowEntry of safeDirectoryEntries(runsRoot, `Python state runs root ${stateRoot}`)) {
    if (workflowEntry.isSymbolicLink() || !workflowEntry.isDirectory()) continue;
    const workflowRoot = path.join(runsRoot, workflowEntry.name);
    for (const runEntry of safeDirectoryEntries(workflowRoot, `Python workflow root ${workflowEntry.name}`)) {
      if (runEntry.isSymbolicLink() || !runEntry.isDirectory()) continue;
      const manifestPath = path.join(workflowRoot, runEntry.name, "manifest.json");
      if (!fs.existsSync(manifestPath)) continue;
      const state = readStrictJsonObject(manifestPath, "Python run manifest");
      if (typeof state.workflow === "string" && typeof state.workflow_id !== "string") continue;
      if (typeof state.workflow_id !== "string") {
        throw new Error(`blocked: ambiguous run manifest runtime: ${manifestPath}`);
      }
      if (
        state.workflow_id !== workflowEntry.name
        || state.run_id !== runEntry.name
        || path.resolve(stateRoot, String(state.run_dir ?? "")) !== path.resolve(path.dirname(manifestPath))
        || typeof state.status !== "string"
      ) {
        throw new Error(`blocked: Python run manifest identity mismatch: ${manifestPath}`);
      }
      if (["complete", "aborted"].includes(state.status)) continue;
      active.push({
        run_id: state.run_id,
        workflow: state.workflow_id,
        task: typeof state.task === "string" ? state.task : "",
        status: state.status,
        runtime: "python",
        workspace_root: pythonWorkspaceForStateRoot(stateRoot, root),
      });
    }
  }
}

function pythonWorkspaceForStateRoot(stateRoot, root) {
  if (samePath(stateRoot, root)) return path.resolve(root);
  const manifestPath = path.join(stateRoot, "manifest.json");
  if (!fs.existsSync(manifestPath)) return null;
  const manifest = readStrictJsonObject(manifestPath, "Python worktree manifest");
  if (typeof manifest.path !== "string" || !manifest.path) {
    throw new Error(`blocked: invalid Python worktree manifest: ${manifestPath}`);
  }
  return path.resolve(root, manifest.path);
}

function readStrictJsonObject(pathName, label) {
  const stat = fs.lstatSync(pathName);
  if (stat.isSymbolicLink() || !stat.isFile()) throw new Error(`blocked: unsafe ${label}: ${pathName}`);
  try {
    const value = JSON.parse(fs.readFileSync(pathName, "utf8"));
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("expected object");
    return value;
  } catch (error) {
    throw new Error(`blocked: unreadable ${label} ${pathName}: ${error.message}`);
  }
}

function safeDirectoryEntries(directory, label) {
  const stat = fs.lstatSync(directory);
  if (stat.isSymbolicLink() || !stat.isDirectory()) {
    throw new Error(`blocked: unsafe ${label}: ${directory}`);
  }
  return fs.readdirSync(directory, { withFileTypes: true });
}

function readRunStateFile(pathName, label) {
  const stat = fs.lstatSync(pathName);
  if (stat.isSymbolicLink() || !stat.isFile()) {
    throw new Error(`blocked: unsafe ${label}: ${pathName}`);
  }
  let state;
  try {
    state = JSON.parse(fs.readFileSync(pathName, "utf8"));
  } catch (error) {
    throw new Error(`blocked: unreadable ${label} ${pathName}: ${error.message}`);
  }
  if (
    !state
    || typeof state !== "object"
    || typeof state.run_id !== "string"
    || !state.run_id
    || typeof state.workflow !== "string"
    || !state.workflow
    || typeof state.run_dir !== "string"
    || !state.run_dir
    || typeof state.status !== "string"
  ) {
    throw new Error(`blocked: invalid ${label}: ${pathName}`);
  }
  return state;
}

function sameRunIdentity(left, right) {
  return left.run_id === right.run_id
    && left.workflow === right.workflow
    && path.normalize(left.run_dir) === path.normalize(right.run_dir);
}

function resolveGitTopLevel(cwd) {
  const value = gitOutput(cwd, ["rev-parse", "--show-toplevel"]);
  return value ? path.resolve(value) : null;
}

function assertWorkspacePinned(state) {
  if (!state.workspace_root || ["complete", "aborted"].includes(state.status)) {
    return state;
  }
  const current = resolveGitTopLevel(process.cwd()) ?? path.resolve(process.cwd());
  const root = resolveAgentFlowRoot(process.cwd());
  const pinned = path.resolve(root, state.workspace_root);
  assertRunWorkspacePolicy(root, pinned);
  if (samePath(current, root) && !samePath(pinned, root) && registeredManagedWorktree(root, pinned)) {
    return state;
  }
  if (
    !samePath(current, pinned)
    && samePath(pinned, root)
    && current.toString().includes(`${path.sep}.agent-flow${path.sep}worktrees${path.sep}`)
    && registeredManagedWorktree(root, current)
  ) {
    const migrated = { ...state, workspace_root: current };
    writeJson(path.join(resolveRunDir(root, state.run_dir), "manifest.json"), migrated);
    writeJson(currentRunPath(root, migrated), migrated);
    return migrated;
  }
  if (!samePath(current, state.workspace_root)) {
    throw new Error(
      `blocked: active run ${state.run_id} is pinned to ${state.workspace_root}; ` +
      `current workspace is ${current}. cd to the pinned worktree before continuing`,
    );
  }
  return state;
}

function assertRunWorkspacePolicy(root, workspaceRoot) {
  if (configuredWorktreeNaming(root).worktree === "disabled") return;
  if (!resolveGitTopLevel(root)) {
    throw new Error("blocked: active profile requires a registered git worktree, but the project is not a git repository");
  }
  if (samePath(root, workspaceRoot)) {
    throw new Error("blocked: active run workspace_root is the leader checkout while the profile requires a worktree");
  }
  if (!registeredManagedWorktree(root, workspaceRoot)) {
    throw new Error(`blocked: active run workspace_root is not a registered git worktree: ${workspaceRoot}`);
  }
  const branch = gitOutput(workspaceRoot, ["branch", "--show-current"]);
  if (!branch) {
    throw new Error(`blocked: active run workspace_root uses detached HEAD: ${workspaceRoot}`);
  }
  if (["main", "master", "develop"].includes(branch)) {
    throw new Error(`blocked: active run workspace_root uses protected branch ${branch}`);
  }
}

function prepareRunWorkspace(
  root,
  requestedWorkspace,
  task,
  naming = configuredWorktreeNaming(root),
  requestedWorktree = {},
) {
  const gitRoot = resolveGitTopLevel(requestedWorkspace);
  const rootGit = resolveGitTopLevel(root);
  const hasExplicitName = typeof requestedWorktree.name === "string";
  const hasExplicitBranch = typeof requestedWorktree.branch === "string";
  if (!rootGit || !gitRoot) {
    if (hasExplicitName || hasExplicitBranch) {
      throw new Error("worktree runs require a git repository");
    }
    if (naming.worktree !== "disabled") {
      throw new Error("blocked: active profile requires a registered git worktree, but the project is not a git repository");
    }
    return { path: requestedWorkspace, created: false, branch: null, branch_created: false };
  }
  if (hasExplicitName || hasExplicitBranch) {
    const identity = explicitWorkspaceIdentity(
      root,
      hasExplicitName ? requestedWorktree.name : task,
      hasExplicitBranch ? requestedWorktree.branch : null,
      naming,
    );
    if (hasExplicitName) {
      const reusable = matchingRegisteredWorkspace(root, identity);
      if (reusable) {
        ensureWorktreeHostBridge(root, reusable.path);
        return { path: reusable.path, created: false, branch: reusable.branch, branch_created: false };
      }
    }
    if (lstatIfExists(identity.worktreeRoot)) {
      throw new Error(`explicit worktree branch or path already exists: ${identity.branch}`);
    }
    if (gitOutput(root, ["show-ref", "--verify", `refs/heads/${identity.branch}`])) {
      throw new Error(`explicit worktree branch already exists: ${identity.branch}`);
    }
    return createRunWorkspace(root, identity, hasExplicitName ? requestedWorktree.name : task);
  }
  if (!samePath(gitRoot, root)) {
    if (!registeredManagedWorktree(root, gitRoot)) {
      throw new Error(`blocked: current checkout is not a registered git worktree: ${gitRoot}`);
    }
    let branch = gitOutput(gitRoot, ["branch", "--show-current"]);
    if (["main", "master", "develop"].includes(branch)) {
      throw new Error(`blocked: registered worktree uses protected branch ${branch}`);
    }
    if (!branch) {
      const identity = freshWorkspaceIdentity(root, task, naming);
      branch = identity.branch;
      if (!branch.startsWith("feat/")) {
        throw new Error(`blocked: profile worktree branch prefix must be feat/: ${naming.prefix}`);
      }
      const switched = safeSpawnSync("git", ["switch", "-c", branch], {
        cwd: gitRoot,
        encoding: "utf8",
        stdio: ["ignore", "pipe", "pipe"],
      });
      if (switched.error || switched.status !== 0) {
        throw new Error((switched.stderr || switched.stdout || switched.error?.message || "failed to name detached worktree branch").trim());
      }
      ensureWorktreeHostBridge(root, gitRoot);
      ensureWorktreeRegistrationManifest(root, gitRoot, branch, task, naming);
      return { path: gitRoot, created: false, branch, branch_created: true };
    }
    if (!workspaceHasRunHistory(root, gitRoot)) {
      ensureWorktreeHostBridge(root, gitRoot);
      ensureWorktreeRegistrationManifest(root, gitRoot, branch, task, naming);
      return { path: gitRoot, created: false, branch, branch_created: false };
    }
  }
  if (naming.worktree === "disabled") {
    return { path: root, created: false, branch: gitOutput(root, ["branch", "--show-current"]), branch_created: false };
  }
  const identity = freshWorkspaceIdentity(root, task, naming);
  return createRunWorkspace(root, identity, task);
}

function createRunWorkspace(root, identity, requestedName) {
  const { slug, branch, worktreeRoot } = identity;
  if (!branch.startsWith("feat/")) {
    throw new Error(`blocked: worktree branch must start with feat/: ${branch}`);
  }
  fs.mkdirSync(path.dirname(worktreeRoot), { recursive: true });
  const baseRef = configuredRunBase(root, root).base_ref;
  const args = ["worktree", "add", "-b", branch, worktreeRoot, baseRef];
  const result = safeSpawnSync("git", args, {
    cwd: root,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    timeout: 300_000,
  });
  if (result.error || result.status !== 0) {
    throw new Error((result.stderr || result.stdout || result.error?.message || "git worktree add failed").trim());
  }
  const workspace = {
    path: worktreeRoot,
    created: true,
    branch,
    branch_created: true,
    registration_manifest: worktreeRegistrationManifestPath(root, `feat-${slug}`),
  };
  try {
    ensureWorktreeHostBridge(root, worktreeRoot);
    writeWorktreeRegistrationManifest(root, workspace, requestedName);
  } catch (error) {
    const cleanupError = cleanupCreatedRunWorkspace(root, workspace);
    if (cleanupError) {
      throw new Error(`${error instanceof Error ? error.message : String(error)}; cleanup failed: ${cleanupError}`);
    }
    throw error;
  }
  return workspace;
}

function explicitWorkspaceIdentity(root, name, requestedBranch, naming) {
  const normalizedName = String(name).normalize("NFKC").trim().replace(/^feat-/iu, "");
  if (!/[\p{L}\p{N}]/u.test(normalizedName)) {
    throw new Error(`worktree name must contain at least one safe character: ${name}`);
  }
  const slug = semanticWorktreeSlug(name, naming.max_slug_length);
  const branch = requestedBranch ?? `${naming.prefix}${slug}`;
  const checked = safeSpawnSync("git", ["check-ref-format", "--branch", branch], {
    cwd: root,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
  if (checked.error || checked.status !== 0) {
    throw new Error(`invalid worktree branch: ${branch}`);
  }
  if (["main", "master", "develop"].includes(branch)) {
    throw new Error(`protected worktree branch is not allowed: ${branch}`);
  }
  if (!branch.startsWith("feat/")) {
    throw new Error(`worktree branch must start with feat/: ${branch}`);
  }
  return {
    slug,
    name: `feat-${slug}`,
    branch,
    worktreeRoot: path.join(root, ".agent-flow", "worktrees", `feat-${slug}`),
  };
}

function matchingRegisteredWorkspace(root, identity) {
  const canonical = worktreeRegistrationManifestPath(root, identity.name);
  const legacy = path.join(root, ".agent-flow", "worktrees", identity.name, "manifest.json");
  const manifestPath = lstatIfExists(canonical) ? canonical : legacy;
  const stat = lstatIfExists(manifestPath);
  if (!stat || stat.isSymbolicLink() || !stat.isFile()) return null;
  let payload;
  try {
    payload = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  } catch {
    return null;
  }
  if (
    !payload
    || typeof payload !== "object"
    || Array.isArray(payload)
    || payload.name !== identity.name
    || payload.branch !== identity.branch
    || typeof payload.path !== "string"
    || !payload.path
  ) {
    return null;
  }
  const workspaceRoot = path.isAbsolute(payload.path)
    ? path.resolve(payload.path)
    : path.resolve(root, payload.path);
  if (!registeredManagedWorktree(root, workspaceRoot)) return null;
  if (gitOutput(workspaceRoot, ["branch", "--show-current"]) !== identity.branch) return null;
  return { path: workspaceRoot, branch: identity.branch };
}

function worktreeRegistrationManifestPath(root, name) {
  const commonDir = gitOutput(root, ["rev-parse", "--git-common-dir"]);
  const agentFlowRoot = commonDir
    ? path.join(path.resolve(root, commonDir), "agent-flow")
    : path.join(root, ".agent-flow");
  return path.join(agentFlowRoot, "worktrees", name, "manifest.json");
}

function writeWorktreeRegistrationManifest(root, workspace, requestedName) {
  const name = workspace.name ?? path.basename(workspace.path);
  let relativePath = path.relative(root, workspace.path);
  if (relativePath.startsWith("..") || path.isAbsolute(relativePath)) relativePath = workspace.path;
  writeJson(workspace.registration_manifest, {
    name,
    branch: workspace.branch,
    path: relativePath,
    exists: true,
    branch_created_by_agent_flow: workspace.branch_created,
    requested_name: requestedName,
    leader_root: root,
  });
}

function ensureWorktreeRegistrationManifest(root, workspaceRoot, branch, requestedName, naming) {
  const existingRuntime = worktreeRuntimeForWorkspace(root, workspaceRoot);
  if (existingRuntime) {
    const existing = readStrictJsonObject(
      path.join(existingRuntime, "manifest.json"),
      "worktree registration manifest",
    );
    if (
      existing.name !== path.basename(existingRuntime)
      || existing.branch !== branch
      || gitOutput(workspaceRoot, ["branch", "--show-current"]) !== branch
    ) {
      throw new Error(`blocked: registered worktree identity changed: ${workspaceRoot}`);
    }
    return existingRuntime;
  }
  if (typeof branch !== "string" || !branch.startsWith("feat/")) {
    throw new Error(`blocked: registered worktree branch must start with feat/: ${branch}`);
  }
  const slug = semanticWorktreeSlug(branch.slice("feat/".length), naming.max_slug_length);
  const name = `feat-${slug}`;
  const registrationManifest = worktreeRegistrationManifestPath(root, name);
  const registrationRoot = path.dirname(registrationManifest);
  if (lstatIfExists(registrationRoot) || lstatIfExists(registrationManifest)) {
    throw new Error(`blocked: worktree registration name already belongs to another checkout: ${name}`);
  }
  writeWorktreeRegistrationManifest(root, {
    name,
    path: workspaceRoot,
    branch,
    branch_created: false,
    registration_manifest: registrationManifest,
  }, requestedName);
  const registeredRuntime = worktreeRuntimeForWorkspace(root, workspaceRoot);
  if (!registeredRuntime || !samePath(registeredRuntime, registrationRoot)) {
    throw new Error(`blocked: worktree registration could not be authenticated: ${workspaceRoot}`);
  }
  return registeredRuntime;
}

function workspaceHasRunHistory(root, workspaceRoot) {
  if (scanRunManifests(root).some((state) => (
    typeof state.workspace_root === "string" && samePath(state.workspace_root, workspaceRoot)
  ))) {
    return true;
  }
  const runtimeRoot = worktreeRuntimeForWorkspace(root, workspaceRoot);
  return runtimeRoot ? pythonRuntimeHasRunHistory(runtimeRoot) : false;
}

function worktreeRuntimeForWorkspace(root, workspaceRoot) {
  const commonDir = gitOutput(root, ["rev-parse", "--git-common-dir"]);
  if (!commonDir) return null;
  const registrationsRoot = path.join(path.resolve(root, commonDir), "agent-flow", "worktrees");
  const registrationsStat = lstatIfExists(registrationsRoot);
  if (!registrationsStat) return null;
  if (registrationsStat.isSymbolicLink() || !registrationsStat.isDirectory()) {
    throw new Error(`blocked: unsafe worktree registration root: ${registrationsRoot}`);
  }
  const entries = safeDirectoryEntries(registrationsRoot, "worktree registration root")
    .filter((entry) => entry.isDirectory() && !entry.isSymbolicLink())
    .sort((left, right) => left.name.localeCompare(right.name));
  for (const entry of entries) {
    const runtimeRoot = path.join(registrationsRoot, entry.name);
    const manifestPath = path.join(runtimeRoot, "manifest.json");
    const manifestStat = lstatIfExists(manifestPath);
    if (!manifestStat) continue;
    const manifest = readStrictJsonObject(manifestPath, "worktree registration manifest");
    if (manifest.name !== entry.name || typeof manifest.path !== "string" || !manifest.path) {
      throw new Error(`blocked: invalid worktree registration manifest: ${manifestPath}`);
    }
    if (
      typeof manifest.leader_root !== "string"
      || !samePath(manifest.leader_root, root)
      || typeof manifest.branch !== "string"
      || !manifest.branch
    ) {
      throw new Error(`blocked: invalid worktree registration leader: ${manifestPath}`);
    }
    const checkout = path.isAbsolute(manifest.path)
      ? path.resolve(manifest.path)
      : path.resolve(root, manifest.path);
    if (samePath(checkout, workspaceRoot)) {
      if (
        !registeredManagedWorktree(root, checkout)
        || gitOutput(checkout, ["branch", "--show-current"]) !== manifest.branch
      ) {
        throw new Error(`blocked: worktree registration no longer matches git: ${manifestPath}`);
      }
      return runtimeRoot;
    }
  }
  return null;
}

function nodeStateRootForWorkspace(root, workspaceRoot) {
  if (samePath(root, workspaceRoot)) return path.resolve(root);
  const runtimeRoot = worktreeRuntimeForWorkspace(root, workspaceRoot);
  if (!runtimeRoot) {
    throw new Error(`blocked: registered worktree has no git-private runtime root: ${workspaceRoot}`);
  }
  return runtimeRoot;
}

function pythonRuntimeHasRunHistory(runtimeRoot) {
  const runsRoot = path.join(runtimeRoot, ".agent-flow", "runs");
  const runsStat = lstatIfExists(runsRoot);
  if (!runsStat) return false;
  if (runsStat.isSymbolicLink() || !runsStat.isDirectory()) {
    throw new Error(`blocked: unsafe Python worktree runs root: ${runsRoot}`);
  }
  for (const first of safeDirectoryEntries(runsRoot, "Python worktree runs root")) {
    if (!first.isDirectory() || first.isSymbolicLink()) continue;
    const firstRoot = path.join(runsRoot, first.name);
    const legacyMeta = lstatIfExists(path.join(firstRoot, "meta.json"));
    if (legacyMeta?.isFile() && !legacyMeta.isSymbolicLink()) return true;
    for (const second of safeDirectoryEntries(firstRoot, `Python worktree run group ${first.name}`)) {
      if (!second.isDirectory() || second.isSymbolicLink()) continue;
      const manifest = lstatIfExists(path.join(firstRoot, second.name, "manifest.json"));
      if (manifest?.isFile() && !manifest.isSymbolicLink()) return true;
    }
  }
  return false;
}

function freshWorkspaceIdentity(root, task, naming) {
  const baseSlug = semanticWorktreeSlug(task, naming.max_slug_length);
  const baseCodePoints = Array.from(baseSlug);
  const limit = Math.max(12, Math.min(Number(naming.max_slug_length) || 60, 100));
  for (let counter = 1; ; counter += 1) {
    const suffix = counter === 1 ? "" : `-${counter}`;
    const stem = baseCodePoints
      .slice(0, Math.max(1, limit - suffix.length))
      .join("")
      .replace(/-+$/g, "") || "task";
    const slug = `${stem}${suffix}`;
    const branch = `${naming.prefix}${slug}`;
    const worktreeRoot = path.join(root, ".agent-flow", "worktrees", `feat-${slug}`);
    const runtimeRoot = path.dirname(
      worktreeRegistrationManifestPath(root, `feat-${slug}`),
    );
    const branchExists = Boolean(gitOutput(root, ["show-ref", "--verify", `refs/heads/${branch}`]));
    if (!branchExists && !lstatIfExists(worktreeRoot) && !lstatIfExists(runtimeRoot)) {
      return { slug, branch, worktreeRoot };
    }
  }
}

function assertStartWorkspaceSupported(root, naming = configuredWorktreeNaming(root)) {
  if (naming.worktree !== "disabled" && !resolveGitTopLevel(root)) {
    throw new Error("blocked: active profile requires a registered git worktree, but the project is not a git repository");
  }
}

function cleanupCreatedRunWorkspace(root, workspace) {
  const removed = safeSpawnSync("git", ["worktree", "remove", "--force", workspace.path], {
    cwd: root,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    timeout: 300_000,
  });
  if (removed.error || removed.status !== 0) {
    return (removed.stderr || removed.stdout || removed.error?.message || "git worktree remove failed").trim();
  }
  if (workspace.branch_created) {
    const branch = safeSpawnSync("git", ["branch", "-D", workspace.branch], {
      cwd: root,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    });
    if (branch.error || branch.status !== 0) {
      return (branch.stderr || branch.stdout || branch.error?.message || "git branch cleanup failed").trim();
    }
  }
  if (workspace.registration_manifest) {
    try {
      fs.rmSync(workspace.registration_manifest, { force: true });
      fs.rmdirSync(path.dirname(workspace.registration_manifest));
    } catch (error) {
      if (!error || !["ENOENT", "ENOTEMPTY"].includes(error.code)) {
        return `worktree manifest cleanup failed: ${error instanceof Error ? error.message : String(error)}`;
      }
    }
  }
  return "";
}

function configuredWorktreeNaming(root) {
  const kitPath = path.join(root, ".agent-flow", "kit.json");
  const kit = lstatIfExists(kitPath) ? readInstalledKit(root) : {};
  const profile = configuredPrimaryProfile(kit);
  const profilePath = path.join(root, ".agent-flow", "profiles", `${profile}.yaml`);
  const result = { prefix: "feat/", max_slug_length: 60, worktree: "required" };
  if (!fs.existsSync(profilePath)) {
    if (hasConfiguredProfile(kit)) {
      throw new Error(`blocked: unknown installed profile: ${profile}`);
    }
    return result;
  }
  const payload = readInstalledProfilePayload(root, profile);
  const branching = payload.branching;
  if (branching && typeof branching === "object" && !Array.isArray(branching)) {
    if (typeof branching.worktree === "string") result.worktree = branching.worktree;
    const naming = branching.naming;
    if (naming && typeof naming === "object" && !Array.isArray(naming)) {
      if (typeof naming.prefix === "string") result.prefix = naming.prefix;
      if (Number.isInteger(naming.max_slug_length)) result.max_slug_length = naming.max_slug_length;
    }
  }
  return result;
}

function configuredPrimaryProfile(kit) {
  let profiles = [];
  if (kit?.profiles !== undefined && kit?.profiles !== null) {
    if (!Array.isArray(kit.profiles)) {
      throw new Error("blocked: installed kit profiles must be a list");
    }
    profiles = kit.profiles.map((profile) => validateConfiguredProfile(profile, "profiles"));
  }
  const legacyProfile = kit?.profile !== undefined && kit?.profile !== null
    ? validateConfiguredProfile(kit.profile, "profile")
    : null;
  if (kit?.primary_profile !== undefined && kit?.primary_profile !== null) {
    const primaryProfile = validateConfiguredProfile(kit.primary_profile, "primary_profile");
    if (profiles.length > 0 && !profiles.includes(primaryProfile)) {
      throw new Error("blocked: installed kit primary_profile must be contained in profiles");
    }
    return primaryProfile;
  }
  if (legacyProfile && legacyProfile !== "generic" && profiles.includes(legacyProfile)) {
    return legacyProfile;
  }
  if (profiles.length > 0) return profiles[0];
  return legacyProfile || "generic";
}

function installPrimaryProfile(detectedProfile, selection, existingKit) {
  if (selection?.explicitProfileSelection) {
    return validateConfiguredProfile(selection.profiles?.[0], "primary_profile");
  }
  if (
    selection?.profileSelection === "explicit"
  ) {
    if (existingKit && typeof existingKit === "object" && !Array.isArray(existingKit)) {
      return configuredPrimaryProfile(existingKit);
    }
    return validateConfiguredProfile(selection.profiles?.[0], "primary_profile");
  }
  return validateConfiguredProfile(detectedProfile, "primary_profile");
}

function validateConfiguredProfile(profile, field) {
  if (typeof profile !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(profile)) {
    throw new Error(`blocked: invalid profile name in installed kit ${field}: ${JSON.stringify(profile)}`);
  }
  return profile;
}

function readInstalledProfileText(root, profile) {
  return readInstalledProfileSnapshot(root, profile).text;
}

function readInstalledProfilePayload(root, profile) {
  return readInstalledProfileSnapshot(root, profile).payload;
}

function readInstalledProfileSnapshot(root, profile) {
  const profilePath = path.join(root, ".agent-flow", "profiles", `${profile}.yaml`);
  requireInstalledRegularFile(root, profilePath, `installed profile ${profile}`);
  const text = fs.readFileSync(profilePath, "utf8");
  const digest = crypto.createHash("sha256").update(text).digest("hex");
  const cacheKey = `${path.resolve(profilePath)}\0${digest}`;
  const cached = installedProfileSnapshotCache.get(cacheKey);
  if (cached) return cached;

  let payload;
  try {
    payload = parseInstalledProfileYaml(text, profile, profilePath);
  } catch (error) {
    throw new Error(`blocked: installed profile is invalid YAML or shape: ${profile}: ${error.message}`);
  }
  const snapshot = { text, payload };
  installedProfileSnapshotCache.set(cacheKey, snapshot);
  return snapshot;
}

function installedProfilePromptSelection(root) {
  const kit = readInstalledKit(root);
  let profileIds = [];
  if (kit.profiles !== undefined && kit.profiles !== null) {
    if (!Array.isArray(kit.profiles)) {
      throw new Error("blocked: installed kit profiles must be a list");
    }
    profileIds = [...new Set(kit.profiles.map((profile) => validateConfiguredProfile(profile, "profiles")))];
  }
  const primaryProfile = configuredPrimaryProfile(kit);
  if (profileIds.length === 0) profileIds = [primaryProfile];
  if (!profileIds.includes(primaryProfile)) {
    throw new Error("blocked: installed kit primary_profile must be contained in profiles");
  }
  return { profileIds, primaryProfile };
}

function installedProfilePromptBlocks(root, selection = null) {
  const resolved = selection ?? installedProfilePromptSelection(root);
  const profileIds = [...new Set(
    (resolved.profileIds ?? []).map((profile) => validateConfiguredProfile(profile, "profiles")),
  )];
  const primaryProfile = validateConfiguredProfile(resolved.primaryProfile, "primary_profile");
  if (profileIds.length === 0 || !profileIds.includes(primaryProfile)) {
    throw new Error("blocked: installed prompt profile selection is inconsistent");
  }
  const [profileId, snapshot] = mergeProfilePayloads(
    profileIds.map((profile) => [profile, readInstalledProfilePayload(root, profile)]),
    primaryProfile,
  );
  const blocks = {
    architecture: renderProfilePromptBlock(profileId, snapshot, "slice-plan"),
    compact: renderProfilePromptBlock(profileId, snapshot, "implement"),
  };
  if (
    !blocks
    || typeof blocks !== "object"
    || Array.isArray(blocks)
    || typeof blocks.architecture !== "string"
    || typeof blocks.compact !== "string"
  ) {
    throw new Error("blocked: installed profile prompt renderer returned an invalid shape");
  }
  return blocks;
}

function profilePromptBlockForPhase(blocks, phaseId) {
  return PROFILE_ARCHITECTURE_PHASES.has(phaseId) ? blocks.architecture : blocks.compact;
}

function installedProfilePromptBlock(root, phaseId) {
  return profilePromptBlockForPhase(installedProfilePromptBlocks(root), phaseId);
}

function hasConfiguredProfile(kit) {
  if (kit?.primary_profile !== undefined && kit?.primary_profile !== null) return true;
  return kit?.profiles !== undefined && kit?.profiles !== null
    ? Array.isArray(kit.profiles) && kit.profiles.length > 0
    : kit?.profile !== undefined && kit?.profile !== null;
}

function registeredManagedWorktree(root, worktreeRoot) {
  const result = safeSpawnSync("git", ["worktree", "list", "--porcelain"], {
    cwd: root,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
  });
  if (result.error || result.status !== 0) return false;
  return result.stdout.split(/\n\n+/).some((block) => {
    const lines = block.split(/\r?\n/);
    const worktree = lines.find((line) => line.startsWith("worktree "))?.slice(9);
    return worktree && samePath(worktree, worktreeRoot);
  });
}

function currentSkillPlanHash(root) {
  const index = readInstalledSkillIndex(root);
  const computed = computeSkillPlanHash(index, root, true);
  const kit = readInstalledKit(root);
  assertInstalledProfileSelection(root, kit, index);
  assertInstalledAndroidOfficialProvenance(root, index);
  assertManagedHostFilesInstalled(root, kit);
  if (kit?.skill_plan_hash_version === 2 && kit.skill_plan_hash !== computed) {
    throw new Error("blocked: installed skill index or snapshot no longer matches kit.json");
  }
  assertCommittedSkillHostLinksApplied(
    root,
    index,
    authenticatedPreviousSkillLinks(kit, index),
  );
  return computed;
}

function assertInstalledAndroidOfficialProvenance(root, index) {
  const selection = index?.selection;
  if (!selection || typeof selection !== "object" || Array.isArray(selection)) return;
  const configured = Array.isArray(selection.skill_profiles) && selection.skill_profiles.length > 0
    ? selection.skill_profiles
    : selection.profiles;
  if (!Array.isArray(configured) || !configured.some((value) => portableSkillCasefold(value) === "android")) {
    return;
  }
  const agentFlowRoot = path.join(root, ".agent-flow");
  const lock = readStrictInstalledObject(
    root,
    path.join(agentFlowRoot, "skills", "upstream-lock.json"),
    "installed upstream skill lock",
  );
  if (
    !lock.android_official
    || typeof lock.android_official !== "object"
    || Array.isArray(lock.android_official)
  ) {
    throw new Error("blocked: installed Android official skill lock is missing");
  }
  requireInstalledRegularFile(
    root,
    path.join(agentFlowRoot, "skills", "source-policy.yaml"),
    "installed skill source policy",
  );
  requireInstalledRegularFile(
    root,
    path.join(agentFlowRoot, "profiles", "android.yaml"),
    "installed Android profile",
  );
  const licenseReference = lock.android_official.license_reference;
  if (
    typeof licenseReference === "string"
    && licenseReference.length > 0
    && !path.isAbsolute(licenseReference)
    && !licenseReference.includes("\\")
    && licenseReference.split("/").every((part) => part && part !== "." && part !== "..")
  ) {
    requireInstalledRegularFile(
      root,
      path.join(agentFlowRoot, "skills", ...licenseReference.split("/")),
      "installed Android official license",
    );
  }
  validateAndroidOfficialLock(agentFlowRoot, lock.android_official);
}

function assertSkillPlanPinned(state, root) {
  const hasMainHash = typeof state.skill_plan_hash === "string" && state.skill_plan_hash.length > 0;
  const hasMainFields = Object.hasOwn(state, "skill_plan_hash") || Object.hasOwn(state, "skill_plan_hash_version");
  if (!hasMainHash) {
    if (hasMainFields) {
      throw new Error("blocked: active run has an invalid skill plan pin");
    }
    throw new Error(
      "blocked: an active legacy run is missing its skill plan pin; its original snapshot cannot be reconstructed safely",
    );
  }
  if (state.skill_plan_hash_version !== 2) {
    throw new Error(
      "blocked: an active legacy run has an obsolete skill plan pin; its original snapshot cannot be reconstructed safely",
    );
  }
  if (currentSkillPlanHash(root) !== state.skill_plan_hash) {
    throw new Error("blocked: installed skill plan changed during the active run; restore the pinned snapshot or start a new run");
  }
  if (state.local_skill_plan_hash) {
    if (
      state.local_skill_plan_hash_version !== LOCAL_SKILL_PLAN_HASH_VERSION
      || projectLocalSkillPlanHash(root) !== state.local_skill_plan_hash
    ) {
      throw new Error(
        "blocked: project-local skill plan changed during the active run; restore it or start a new run",
      );
    }
  } else if (Object.hasOwn(state, "local_skill_plan_hash") || Object.hasOwn(state, "local_skill_plan_hash_version")) {
    throw new Error("blocked: active run has an invalid project-local skill plan pin");
  } else {
    throw new Error(
      "blocked: an active legacy run is missing its project-local skill plan pin; its original snapshot cannot be reconstructed safely",
    );
  }
  return state;
}

function computeSkillPlanHash(index, root, verifyTrees = false) {
  const skills = (index?.skills || []).map((skill) => {
    const skillPath = path.resolve(root, String(skill.path || ""));
    const relative = path.relative(root, skillPath);
    if (path.basename(skillPath) !== "SKILL.md" || relative.startsWith("..") || path.isAbsolute(relative)) {
      throw new Error(`blocked: invalid installed skill path: ${skill.name}`);
    }
    const recordedHash = requireProjectSkillTreeHash(skill.tree_hash, skill.name);
    if (verifyTrees) {
      requireInstalledRegularFile(root, skillPath, `installed skill snapshot ${skill.name}`);
    }
    const liveHash = verifyTrees ? hashSkillTree(path.dirname(skillPath)) : recordedHash;
    if (verifyTrees && skill.tree_hash !== liveHash) {
      throw new Error(`blocked: installed skill snapshot changed: ${skill.name}`);
    }
    const record = [
      skill.name,
      relative.split(path.sep).join("/"),
      skill.source,
      skill.source_host ?? null,
      liveHash,
      [...(skill.profiles || [])].sort(compareCodePoints),
    ];
    if (
      Object.hasOwn(skill, "activation")
      || Object.hasOwn(skill, "taskTerms")
      || Object.hasOwn(skill, "pathGlobs")
    ) {
      record.push({
        activation: skill.activation ?? null,
        workflowPhases: normalizedRoutingHashStrings(skill, "workflowPhases"),
        taskTerms: normalizedRoutingHashStrings(skill, "taskTerms"),
        pathGlobs: normalizedRoutingHashStrings(skill, "pathGlobs"),
      });
    }
    return record;
  }).sort((a, b) => compareCodePoints(a[0], b[0]));
  const normalized = {
    profiles: [...(index?.selection?.profiles || [])].sort(compareCodePoints),
    skill_profiles: [...(index?.selection?.skill_profiles || [])].sort(compareCodePoints),
    explicit_skills: [...(index?.selection?.explicit_skills || [])].sort(compareCodePoints),
    ...(Object.hasOwn(index?.selection || {}, "external_exposure_skills")
      ? {
          external_exposure_skills: normalizedExternalExposureSkillNames(
            index.selection,
            { legacyFallback: false },
          ).sort(compareCodePoints),
        }
      : {}),
    ...(Object.hasOwn(index?.selection || {}, "profile_selection")
      ? { profile_selection: index.selection.profile_selection }
      : {}),
    required_review: Object.fromEntries(
      Object.entries(index?.selection?.required_review || {})
        .sort(([a], [b]) => compareCodePoints(a, b))
        .map(([profile, names]) => [profile, [...names].sort(compareCodePoints)]),
    ),
    conditional_skills: index?.selection?.conditional_skills || {},
    profile_routing: index?.selection?.profile_routing || {},
    skills,
  };
  return crypto.createHash("sha256").update(JSON.stringify(normalized)).digest("hex");
}

function normalizedRoutingHashStrings(skill, key) {
  const value = skill[key] ?? [];
  if (!Array.isArray(value) || value.some((entry) => typeof entry !== "string")) {
    throw new Error(`blocked: installed skill has invalid ${key}: ${skill.name}`);
  }
  return [...value].sort(compareCodePoints);
}

function compareCodePoints(left, right) {
  const a = Array.from(String(left), (char) => char.codePointAt(0));
  const b = Array.from(String(right), (char) => char.codePointAt(0));
  for (let index = 0; index < Math.min(a.length, b.length); index += 1) {
    if (a[index] !== b[index]) return a[index] - b[index];
  }
  return a.length - b.length;
}

function resolveRunDir(root, runDir) {
  return path.isAbsolute(runDir) ? runDir : path.join(root, runDir);
}

function assertInstalled(root) {
  const kitPath = path.join(root, ".agent-flow", "kit.json");
  const skillIndexPath = path.join(root, ".agent-flow", "skills", "index.json");
  if (!lstatIfExists(kitPath) || !lstatIfExists(skillIndexPath)) {
    throw new Error("agent-flow is not installed; run agent-flow install first");
  }
  const kit = readInstalledKit(root);
  const skillIndex = readInstalledSkillIndex(root);
  const phases = fullFeaturePhases();
  try {
    assertProjectRuntimeInstalled(root, kit);
  } catch (error) {
    throw new Error(`blocked: installed project runtime is invalid: ${error instanceof Error ? error.message : String(error)}`);
  }
  assertInstalledProfileSelection(root, kit, skillIndex);
  assertManagedHostFilesInstalled(root, kit);
  const selectedSkillPaths = skillIndex.skills
    .map((skill) => selectedSkillPath(root, skill))
    .filter(Boolean);
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
    path.join(root, ".omp", "agents", "code-reviewer.md"),
  ];
  const missing = required.filter((pathName) => !fs.existsSync(pathName));
  if (missing.length > 0) {
    throw new Error(`agent-flow is not installed. run: agent-flow-kit install`);
  }
  for (const codeReviewer of [
    path.join(root, ".Codex", "agents", "code-reviewer.md"),
    path.join(root, ".claude", "agents", "code-reviewer.md"),
    path.join(root, ".omp", "agents", "code-reviewer.md"),
  ]) {
    if (!fs.readFileSync(codeReviewer, "utf8").trim()) {
      throw new Error(`agent-flow is not installed correctly: ${path.relative(root, codeReviewer)} is empty`);
    }
  }
}

function assertManagedHostFilesInstalled(root, kit) {
  assertManagedHostFilesCommitment(kit, { required: true });
  const provenance = readManagedHostFileProvenance(kit, { required: true });
  for (const relative of REQUIRED_MANAGED_HOST_FILES) {
    if (!provenance.has(relative)) {
      throw new Error(`blocked: installed managed host file provenance is missing: ${relative}`);
    }
  }
  for (const [relative, entry] of provenance) {
    const destination = path.join(root, ...relative.split("/"));
    requireInstalledRegularFile(root, destination, `managed host file ${relative}`);
    if (sha256Bytes(fs.readFileSync(destination)) !== entry.sha256) {
      throw new Error(`blocked: installed managed host file changed: ${relative}`);
    }
  }
  assertManagedHookContractInstalled(root, kit);
}

function readInstalledKit(root) {
  return readStrictInstalledObject(
    root,
    path.join(root, ".agent-flow", "kit.json"),
    "installed kit metadata",
  );
}

function readInstalledSkillIndex(root) {
  const indexPath = path.join(root, ".agent-flow", "skills", "index.json");
  const index = readStrictInstalledObject(root, indexPath, "installed skill index");
  if (!Array.isArray(index.skills)) {
    throw new Error(`blocked: installed skill index has invalid skills: ${indexPath}`);
  }
  return index;
}

function readStrictInstalledObject(root, pathName, label) {
  requireInstalledRegularFile(root, pathName, label);
  try {
    const value = JSON.parse(fs.readFileSync(pathName, "utf8"));
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new Error("expected object");
    }
    return value;
  } catch (error) {
    throw new Error(`blocked: ${label} is unreadable: ${pathName}: ${error.message}`);
  }
}

function assertInstalledProfileSelection(root, kit, index) {
  const kitProfiles = kit?.profiles;
  const indexProfiles = index?.selection?.profiles;
  if (!Array.isArray(kitProfiles) || !Array.isArray(indexProfiles)) {
    throw new Error("blocked: installed kit profiles do not match the skill index selection");
  }
  if (kit?.profile !== undefined && kit?.profile !== null) {
    validateConfiguredProfile(kit.profile, "profile");
  }
  const kitSelection = kit?.profile_selection;
  const indexSelection = index?.selection?.profile_selection;
  if (
    (kitSelection !== undefined || indexSelection !== undefined)
    && (
      !["auto", "explicit"].includes(kitSelection)
      || kitSelection !== indexSelection
    )
  ) {
    throw new Error("blocked: installed kit profile selection does not match the skill index selection");
  }
  for (const profile of kitProfiles) validateConfiguredProfile(profile, "profiles");
  for (const profile of indexProfiles) {
    if (typeof profile !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(profile)) {
      throw new Error(`blocked: invalid profile name in installed skill index: ${JSON.stringify(profile)}`);
    }
  }
  if (
    kitProfiles.length !== indexProfiles.length
    || kitProfiles.some((profile, indexValue) => profile !== indexProfiles[indexValue])
  ) {
    throw new Error("blocked: installed kit profiles do not match the skill index selection");
  }
  const primaryProfile = configuredPrimaryProfile(kit);
  for (const profile of new Set([...kitProfiles, primaryProfile])) {
    readInstalledProfileText(root, profile);
  }
}

function requireInstalledRegularFile(root, pathName, label) {
  const lexicalRoot = path.resolve(root);
  const lexicalPath = path.resolve(pathName);
  const relative = path.relative(lexicalRoot, lexicalPath);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(`blocked: ${label} escapes the project: ${pathName}`);
  }
  let cursor = lexicalRoot;
  const parts = relative.split(path.sep).filter(Boolean);
  for (let index = 0; index < parts.length; index += 1) {
    cursor = path.join(cursor, parts[index]);
    const stat = lstatIfExists(cursor);
    if (!stat) throw new Error(`blocked: ${label} is unreadable: ${cursor}`);
    if (stat.isSymbolicLink()) throw new Error(`blocked: ${label} may not use symlinks: ${cursor}`);
    const final = index === parts.length - 1;
    if ((final && !stat.isFile()) || (!final && !stat.isDirectory())) {
      throw new Error(`blocked: ${label} has an invalid path component: ${cursor}`);
    }
    if (final && stat.nlink !== 1) {
      throw new Error(`blocked: ${label} may not be hard-linked: ${cursor}`);
    }
  }
  const realRoot = fs.realpathSync(lexicalRoot);
  const realPath = fs.realpathSync(lexicalPath);
  const realRelative = path.relative(realRoot, realPath);
  if (realRelative.startsWith("..") || path.isAbsolute(realRelative)) {
    throw new Error(`blocked: ${label} escapes the project: ${pathName}`);
  }
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

function normalizeRunState(root, state) {
  if (state.status === "complete" || state.phase === "complete") {
    return state;
  }
  const workspaceRoot = typeof state.workspace_root === "string" && state.workspace_root
    ? path.resolve(root, state.workspace_root)
    : path.resolve(root);
  if (!state.workspace_root && configuredWorktreeNaming(root).worktree !== "disabled") {
    throw new Error("blocked: active run is missing its required registered worktree workspace_root");
  }
  assertRunWorkspacePolicy(root, workspaceRoot);
  const phases = verifiedRunnerWorkflowPhases(state);
  const index = phases.findIndex((phase) => phase.id === state.phase);
  if (index === -1 || index === state.phase_index) {
    return state;
  }
  const normalized = {
    ...state,
    phase_index: index,
  };
  writeJson(path.join(resolveRunDir(root, state.run_dir), "manifest.json"), normalized);
  writeJson(currentRunPath(root, normalized), normalized);
  return normalized;
}

function currentRunPath(root, state = null) {
  const stateRoot = state?.workspace_root
    ? nodeStateRootForWorkspace(root, state.workspace_root)
    : path.resolve(root);
  return path.join(stateRoot, ".agent-flow", "state", "current-run.json");
}

function pushWatchStatePath(root, state = null) {
  const stateRoot = state?.workspace_root
    ? nodeStateRootForWorkspace(root, state.workspace_root)
    : path.resolve(root);
  return path.join(stateRoot, ".agent-flow", "state", "push-watch.json");
}

function currentBranch(root) {
  const result = safeSpawnSync("git", ["branch", "--show-current"], {
    cwd: root,
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

function workspaceRootForState(state, root = null) {
  const base = root ?? resolveAgentFlowRoot(process.cwd());
  return path.resolve(base, state.workspace_root ?? ".");
}

function pinnedLedgerMode(state) {
  try {
    return resolveLedgerMode(state?.ledger_mode);
  } catch (_error) {
    return "artifacts-only";
  }
}

function ledgerPromptRound(state) {
  return Math.min(3, Math.max(1, Number(state?.fix_loop_rounds ?? 0)));
}

function ledgerCaptureRound(state, routedTo) {
  const current = Math.max(0, Number(state?.fix_loop_rounds ?? 0));
  const prospective = routedTo === "fix-loop" ? current + 1 : Math.max(1, current);
  return Math.min(3, Math.max(1, prospective));
}

function phaseRevision(state) {
  const revision = Number(state?.phase_revision ?? 0);
  return Number.isSafeInteger(revision) && revision >= 0 ? revision : 0;
}

function transitionOccurrenceId(state, phase) {
  return stableControlSha256({
    schema_version: 1,
    run_id: String(state?.run_id ?? ""),
    workflow_id: String(state?.workflow ?? ""),
    phase_id: String(phase?.id ?? ""),
    phase_index: Number(state?.phase_index ?? 0),
    phase_revision: phaseRevision(state),
  });
}

const PENDING_NODE_TRANSITION_VERSION = 2;

function pendingNodeTransitionPath(runDir) {
  return path.join(runDir, "transition-pending.json");
}

function createPendingNodeTransition({
  state,
  nextState,
  nextPhase,
  currentIndex,
  nextIndex,
  transitionedAt,
  routeKey,
  fixLoopRounds,
}) {
  const routedTo = nextPhase?.id ?? "complete";
  const captureFixLoopRounds = Number.isSafeInteger(fixLoopRounds)
    ? Math.max(0, fixLoopRounds)
    : 0;
  const base = {
    schema_version: PENDING_NODE_TRANSITION_VERSION,
    runtime: "node",
    run_id: state.run_id,
    workflow_id: state.workflow,
    current_index: currentIndex,
    next_index: nextIndex,
    from_state: state,
    next_state: nextState,
    from_state_sha256: stableControlSha256(state),
    next_state_sha256: stableControlSha256(nextState),
    capture: {
      phase_id: state.phase,
      route_key: routeKey,
      routed_to: routedTo,
      round: ledgerCaptureRound(state, routedTo),
      fix_loop_rounds: captureFixLoopRounds,
      generated_at: transitionedAt,
      transition_occurrence_id: transitionOccurrenceId(
        state,
        { id: state.phase },
      ),
      committed: false,
    },
    observation: nextPhase ? {
      phase_id: nextPhase.id,
      round: ledgerPromptRound(nextState),
      generated_at: transitionedAt,
      prompt_bytes: Buffer.byteLength(String(nextPhase.instruction ?? ""), "utf8"),
    } : null,
  };
  return { ...base, content_sha256: stableControlSha256(base) };
}

function writePendingNodeTransition(runDir, payload) {
  const journalPath = pendingNodeTransitionPath(runDir);
  if (fs.existsSync(journalPath)) {
    throw new Error(`blocked: unresolved execution ledger transition: ${journalPath}`);
  }
  writeJson(journalPath, payload);
}

function validatePendingNodeTransition(payload, state, phases) {
  if (
    !payload
    || typeof payload !== "object"
    || Array.isArray(payload)
    || payload.schema_version !== PENDING_NODE_TRANSITION_VERSION
    || payload.runtime !== "node"
    || payload.run_id !== state.run_id
    || payload.workflow_id !== state.workflow
    || !Number.isInteger(payload.current_index)
    || !Number.isInteger(payload.next_index)
    || !payload.from_state
    || !payload.next_state
  ) {
    throw new Error("blocked: invalid execution ledger pending transition");
  }
  const { content_sha256: _contentSha256, ...base } = payload;
  if (
    payload.content_sha256 !== stableControlSha256(base)
    || payload.from_state_sha256 !== stableControlSha256(payload.from_state)
    || payload.next_state_sha256 !== stableControlSha256(payload.next_state)
  ) {
    throw new Error("blocked: execution ledger pending transition commitment mismatch");
  }
  const suppliedStateSha256 = stableControlSha256(state);
  if (![payload.from_state_sha256, payload.next_state_sha256].includes(suppliedStateSha256)) {
    throw new Error("blocked: execution ledger pending transition state mismatch");
  }
  const currentPhase = phases[payload.current_index];
  const nextPhase = phases[payload.next_index];
  if (
    payload.from_state.run_id !== payload.run_id
    || payload.next_state.run_id !== payload.run_id
    || payload.from_state.workflow !== payload.workflow_id
    || payload.next_state.workflow !== payload.workflow_id
    || payload.from_state.phase_index !== payload.current_index
    || payload.from_state.phase !== currentPhase?.id
    || payload.next_state.phase_index !== payload.next_index
    || payload.next_state.phase !== (nextPhase?.id ?? "complete")
    || payload.next_state.status !== (nextPhase ? "running" : "complete")
    || payload.from_state.runner_workflow_hash !== payload.next_state.runner_workflow_hash
    || payload.from_state.experiment_enabled !== true
    || payload.next_state.experiment_enabled !== true
    || phaseRevision(payload.next_state) !== phaseRevision(payload.from_state) + 1
  ) {
    throw new Error("blocked: execution ledger pending transition route mismatch");
  }
  const capture = payload.capture;
  const expectedRoutedTo = nextPhase?.id ?? "complete";
  const expectedFixLoopRounds = Number.isSafeInteger(payload.next_state.fix_loop_rounds)
    ? Math.max(0, payload.next_state.fix_loop_rounds)
    : 0;
  const expectedRouteTarget = currentPhase?.routes
    ? (currentPhase.routes[capture?.route_key] ?? currentPhase.routes.default)
    : (phases[payload.current_index + 1]?.id ?? "complete");
  if (
    !capture
    || typeof capture !== "object"
    || Array.isArray(capture)
    || capture.phase_id !== currentPhase?.id
    || typeof capture.route_key !== "string"
    || (!currentPhase?.routes && capture.route_key !== "sequential")
    || expectedRouteTarget !== expectedRoutedTo
    || capture.routed_to !== expectedRoutedTo
    || capture.round !== ledgerCaptureRound(payload.from_state, expectedRoutedTo)
    || capture.fix_loop_rounds !== expectedFixLoopRounds
    || !Number.isSafeInteger(capture.fix_loop_rounds)
    || capture.fix_loop_rounds < 0
    || typeof capture.generated_at !== "string"
    || capture.generated_at !== payload.next_state.updated_at
    || capture.transition_occurrence_id !== transitionOccurrenceId(
      payload.from_state,
      currentPhase,
    )
    || typeof capture.committed !== "boolean"
  ) {
    throw new Error("blocked: invalid execution ledger pending capture");
  }
  if (nextPhase) {
    const observation = payload.observation;
    if (
      !observation
      || observation.phase_id !== nextPhase.id
      || !Number.isInteger(observation.round)
      || typeof observation.generated_at !== "string"
      || !Number.isInteger(observation.prompt_bytes)
      || observation.prompt_bytes < 0
    ) {
      throw new Error("blocked: invalid execution ledger pending observation");
    }
  } else if (payload.observation !== null) {
    throw new Error("blocked: terminal execution ledger transition has an observation");
  }
  return payload;
}

function recoverPendingNodeTransition(root, state, phases) {
  const runDir = resolveRunDir(root, state.run_dir);
  const journalPath = pendingNodeTransitionPath(runDir);
  if (!fs.existsSync(journalPath)) return null;
  let pending = validatePendingNodeTransition(
    readStrictJsonObject(journalPath, "execution ledger pending transition"),
    state,
    phases,
  );
  if (pending.capture.committed !== true) {
    const currentPhase = phases[pending.current_index];
    const capture = captureExecutionState({
      runDir,
      runId: pending.from_state.run_id,
      mode: pinnedLedgerMode(pending.from_state),
      experimentEnabled: true,
      phase: currentPhase,
      artifactPath: path.join(runDir, currentPhase.artifact),
      projectRoot: workspaceRootForState(pending.from_state, root),
      round: pending.capture.round,
      fixLoopRounds: pending.capture.fix_loop_rounds,
      generatedAt: pending.capture.generated_at,
      workflowId: pending.from_state.workflow,
      routeKey: pending.capture.route_key,
      routedTo: pending.capture.routed_to,
      transitionOccurrenceId: pending.capture.transition_occurrence_id,
    });
    committedCaptureResult(capture, true);
    pending = committedPendingNodeCapture(pending);
    writeJson(journalPath, pending);
  }
  let ledgerBlock = "";
  if (pending.observation) {
    const phase = phases[pending.next_index];
    const observation = observeExecutionStateInjection({
      runDir,
      runId: pending.next_state.run_id,
      mode: pinnedLedgerMode(pending.next_state),
      experimentEnabled: true,
      phase,
      projectRoot: workspaceRootForState(pending.next_state, root),
      round: pending.observation.round,
      generatedAt: pending.observation.generated_at,
      promptBytes: pending.observation.prompt_bytes,
    });
    ledgerBlock = committedObservationBlock(observation, true);
  }
  if (process.env.AGENT_FLOW_TEST_FAIL_NODE_TRANSITION_ROUTE_ARTIFACT === "1") {
    throw new Error("injected Node transition route artifact failure");
  }
  syncRouteArtifacts(runDir, phases, pending.current_index, pending.next_index);
  publishPendingNodeTransition(root, runDir, pending);
  fs.unlinkSync(journalPath);
  return { state: pending.next_state, phases, ledgerBlock };
}

function publishPendingNodeTransition(root, runDir, pending) {
  const manifestPath = path.join(runDir, "manifest.json");
  const pointerPath = currentRunPath(root, pending.next_state);
  let manifestPublished = false;
  let pointerPublished = false;
  try {
    writeJson(manifestPath, pending.next_state);
    manifestPublished = true;
    if (process.env.AGENT_FLOW_TEST_FAIL_AFTER_NODE_TRANSITION_MANIFEST === "1") {
      throw new Error("injected Node transition failure after manifest publish");
    }
    writeJson(pointerPath, pending.next_state);
    pointerPublished = true;
  } catch (error) {
    const rollbackErrors = [];
    if (manifestPublished) {
      try {
        writeJson(manifestPath, pending.from_state);
      } catch (rollbackError) {
        rollbackErrors.push(rollbackError);
      }
    }
    if (pointerPublished) {
      try {
        writeJson(pointerPath, pending.from_state);
      } catch (rollbackError) {
        rollbackErrors.push(rollbackError);
      }
    }
    if (rollbackErrors.length > 0) {
      const detail = rollbackErrors
        .map((rollbackError) => rollbackError instanceof Error ? rollbackError.message : String(rollbackError))
        .join("; ");
      throw new Error(`${error instanceof Error ? error.message : String(error)}; canonical rollback failed: ${detail}`);
    }
    throw error;
  }
}

function committedPendingNodeCapture(pending) {
  if (pending?.capture?.committed !== false) {
    throw new Error("blocked: invalid execution ledger pending capture state");
  }
  const { content_sha256: _contentSha256, ...base } = pending;
  const updated = {
    ...base,
    capture: { ...pending.capture, committed: true },
  };
  return { ...updated, content_sha256: stableControlSha256(updated) };
}

function printNodeTransitionResult(result, root) {
  const phase = result.phases[result.state.phase_index];
  if (phase) {
    printNext(result.state, root, {
      ledgerBlock: result.ledgerBlock,
      phases: result.phases,
    });
    return;
  }
  console.log(`workflow complete: ${result.state.run_id}`);
}

function ledgerExperimentControlsFromEnvironment() {
  return {
    experiment_id: process.env.AGENT_FLOW_EXPERIMENT_ID ?? null,
    model_id: process.env.AGENT_FLOW_EXPERIMENT_MODEL_ID ?? null,
    tool_permissions_sha256: process.env.AGENT_FLOW_EXPERIMENT_TOOL_PERMISSIONS_SHA256 ?? null,
    system_prompt_sha256: process.env.AGENT_FLOW_EXPERIMENT_SYSTEM_PROMPT_SHA256 ?? null,
    caps_sha256: process.env.AGENT_FLOW_EXPERIMENT_CAPS_SHA256 ?? null,
    provider_retry_policy_sha256:
      process.env.AGENT_FLOW_EXPERIMENT_PROVIDER_RETRY_POLICY_SHA256 ?? null,
    provider_max_retries: ledgerEnvironmentInteger(
      "AGENT_FLOW_EXPERIMENT_PROVIDER_MAX_RETRIES",
    ),
    pricing_snapshot: ledgerEnvironmentObject(
      "AGENT_FLOW_EXPERIMENT_PRICING_JSON",
    ),
    provider_attestation_key_id:
      process.env.AGENT_FLOW_EXPERIMENT_PROVIDER_ATTESTATION_KEY_ID ?? null,
    provider_attestation_public_key: ledgerEnvironmentObject(
      "AGENT_FLOW_EXPERIMENT_PROVIDER_ATTESTATION_PUBLIC_KEY_JWK",
    ),
  };
}

function ledgerEnvironmentInteger(name) {
  const raw = process.env[name];
  if (raw === undefined) return null;
  if (!/^\d+$/.test(raw)) {
    throw new Error(`invalid ${name}: expected a non-negative integer`);
  }
  const value = Number(raw);
  if (!Number.isSafeInteger(value)) {
    throw new Error(`invalid ${name}: integer exceeds the safe range`);
  }
  return value;
}

function ledgerEnvironmentObject(name) {
  const raw = process.env[name];
  if (raw === undefined) return null;
  let value;
  try {
    value = JSON.parse(raw);
  } catch (error) {
    throw new Error(`invalid ${name}: ${error instanceof Error ? error.message : String(error)}`);
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`invalid ${name}: expected a JSON object`);
  }
  return value;
}

function ledgerObservedRunSnapshot(root, state, phases) {
  const selection = installedProfilePromptSelection(root);
  const [profileId, profile] = mergeProfilePayloads(
    selection.profileIds.map((id) => [id, readInstalledProfilePayload(root, id)]),
    selection.primaryProfile,
  );
  const profileProjection = {
    profile_id: profileId,
    primary_profile: selection.primaryProfile,
    active_profiles: selection.profileIds,
    profile,
  };
  const profileSnapshotSha256 = stableControlSha256(profileProjection);
  return {
    runtime_id: "node",
    profile_snapshot_sha256: profileSnapshotSha256,
    installed_skill_plan_sha256: requireControlCommitment(
      state.skill_plan_hash,
      "installed skill plan",
    ),
    local_skill_plan_sha256: requireControlCommitment(
      state.local_skill_plan_hash,
      "local skill plan",
    ),
    lore_snapshot_sha256: requireControlCommitment(
      state.lore_snapshot_hash,
      "lore snapshot",
    ),
    prompt_controls_sha256: stableControlSha256({
      workflow_phases: phases,
      profile_snapshot_sha256: profileSnapshotSha256,
      installed_skill_plan_sha256: state.skill_plan_hash,
      local_skill_plan_sha256: state.local_skill_plan_hash,
      lore_snapshot_sha256: state.lore_snapshot_hash,
      worktree_mode: state.worktree_mode,
    }),
  };
}

function stableControlSha256(value) {
  return crypto.createHash("sha256").update(JSON.stringify(stableControlValue(value))).digest("hex");
}

function runnerWorkflowHash(workflowId, phases) {
  return stableControlSha256({
    workflow_id: workflowId,
    workflow_phases: phases.map((phase) => ({
      id: phase.id,
      artifact: phase.artifact,
      description: phase.description,
      instruction: phase.instruction,
      required_markers: [...phase.required_markers],
      pause_after: phase.pause_after,
      optional: phase.optional,
      multi_review: phase.multi_review,
      cite_lore: phase.cite_lore,
      routes: phase.routes === null ? null : { ...phase.routes },
    })),
  });
}

function assertRunnerWorkflowPinned(state, phases) {
  if (state?.experiment_enabled !== true) return;
  if (
    state.runner_workflow_hash_version !== 1
    || typeof state.runner_workflow_hash !== "string"
    || !/^[a-f0-9]{64}$/.test(state.runner_workflow_hash)
  ) {
    throw new Error("blocked: active pilot run has an invalid runner workflow snapshot");
  }
  if (state.runner_workflow_hash !== runnerWorkflowHash(state.workflow, phases)) {
    throw new Error("blocked: active pilot runner workflow snapshot changed; restore the installed workflow or start a new run");
  }
}

function verifiedRunnerWorkflowPhases(state) {
  const cached = state?.[VERIFIED_RUN_WORKFLOW_PHASES];
  if (Array.isArray(cached)) {
    assertRunnerWorkflowPinned(state, cached);
    return cached;
  }
  const phases = workflowPhases(state.workflow);
  assertRunnerWorkflowPinned(state, phases);
  Object.defineProperty(state, VERIFIED_RUN_WORKFLOW_PHASES, {
    value: phases,
    enumerable: true,
    configurable: false,
    writable: false,
  });
  return phases;
}

function holdAfterVerifiedWorkflowLoad(root, state) {
  const holdMs = Number.parseInt(process.env.AGENT_FLOW_TEST_HOLD_AFTER_WORKFLOW_LOAD_MS ?? "0", 10);
  if (!Number.isInteger(holdMs) || holdMs <= 0 || holdMs > 10_000) return;
  const marker = path.join(resolveRunDir(root, state.run_dir), "logs", "workflow-snapshot-ready");
  fs.mkdirSync(path.dirname(marker), { recursive: true });
  fs.writeFileSync(marker, "ready\n", "utf8");
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, holdMs);
}

function stableControlValue(value) {
  if (Array.isArray(value)) return value.map(stableControlValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value)
        .sort(compareCodePoints)
        .map((key) => [key, stableControlValue(value[key])]),
    );
  }
  return typeof value === "number" && !Number.isFinite(value) ? null : value;
}

function requireControlCommitment(value, label) {
  if (typeof value !== "string" || !/^[a-f0-9]{64}$/.test(value)) {
    throw new Error(`blocked: invalid ${label} commitment for ledger experiment`);
  }
  return value;
}

function printWorkspacePolicy(workspaceRoot) {
  console.log(`workspace_root: ${workspaceRoot}`);
  console.log(
    `Work cwd policy: keep every source, build, test, and write tool call in ${workspaceRoot}. ` +
    "Workflow transition commands may run from the leader checkout; project work may not.",
  );
}

function committedObservationBlock(observation, experimentEnabled = false) {
  if (experimentEnabled && observation?.ok !== true) {
    throw new Error(
      `execution ledger pilot prompt observation failed: ${observation?.error ?? "unknown error"}; retry the same workflow command`,
    );
  }
  return observation?.ok === true && typeof observation.block === "string"
    ? observation.block
    : "";
}

function committedCaptureResult(capture, experimentEnabled = false) {
  if (experimentEnabled && capture?.ok !== true) {
    throw new Error(
      `execution ledger pilot transition capture failed: ${capture?.error ?? "unknown error"}; retry the same workflow command`,
    );
  }
  return capture;
}

function printNext(state, root = null, {
  ledgerBlock: observedLedgerBlock,
  phases: suppliedPhases,
} = {}) {
  const phases = suppliedPhases ?? verifiedRunnerWorkflowPhases(state);
  assertRunnerWorkflowPinned(state, phases);
  const phase = phases[state.phase_index];
  if (!phase) {
    console.log(`workflow complete: ${state.run_id}`);
    return;
  }
  const localSkillContext = root ? localSkillContextForState(state, root) : {};
  const localSkillBlock = root
    ? localSkillPromptBlock(root, phase.id, localSkillContext.taskScope, localSkillContext.changedFiles)
    : "";
  const profileSkillBlock = root
    ? profileSkillPromptBlock(root, phase.id, state.workspace_root ?? process.cwd(), state.base_commit, state.task)
    : "";
  const profileBlock = root ? installedProfilePromptBlock(root, phase.id) : "";
  const loreBlock = root ? relevantLorePromptBlock(root, phase, state) : "";
  const ledgerBlock = typeof observedLedgerBlock === "string"
    ? observedLedgerBlock
    : root
      ? executionStatePromptBlock({
        runDir: resolveRunDir(root, state.run_dir),
        runId: state.run_id,
        mode: pinnedLedgerMode(state),
        experimentEnabled: state.experiment_enabled === true,
        phase,
        projectRoot: workspaceRootForState(state, root),
        round: ledgerPromptRound(state),
      })
      : "";
  const ledgerSuffix = ledgerBlock ? `\n\n${ledgerBlock}` : "";
  const workspaceRoot = workspaceRootForState(state, root);
  const requiredArtifact = path.resolve(resolveRunDir(root, state.run_dir), phase.artifact);
  console.log(`Current phase: ${phase.id}`);
  console.log(`Run: ${state.run_id}`);
  printWorkspacePolicy(workspaceRoot);
  console.log(`Required artifact: ${requiredArtifact}`);
  console.log(`Instruction: ${phase.instruction}${profileBlock}${profileSkillBlock}${localSkillBlock}${loreBlock}${ledgerSuffix}`);
  if (
    phase.pause_after
    && state.pause_after_pending?.phase === phase.id
    && !pauseAfterApprovalMatches(state, phase, path.join(resolveRunDir(root, state.run_dir), phase.artifact))
  ) {
    console.log(`Approval required: review ${phase.artifact} and wait for explicit user approval before advancing.`);
  } else if (phase.pause_after) {
    console.log(`Pause policy: artifact validation stops at ${phase.id}; wait for explicit user approval before the following advance.`);
  }
  const nextCommand = phase.pause_after
    && fs.existsSync(requiredArtifact)
    && pauseAfterPendingMatches(state, phase, requiredArtifact)
    && !pauseAfterApprovalMatches(state, phase, requiredArtifact)
    ? pauseApprovalCommand()
    : `${AGENT_FLOW_COMMAND} run advance`;
  console.log(`next_command: ${nextCommand}`);
}

function computeStatusPayload(state, root, suppliedPhases = null) {
  const phases = suppliedPhases ?? verifiedRunnerWorkflowPhases(state);
  assertRunnerWorkflowPinned(state, phases);
  const phase = phases[state.phase_index];
  const workspaceRoot = workspaceRootForState(state, root);
  const resolvedRunDir = resolveRunDir(root, state.run_dir);
  const complete = state.status === "complete" || state.phase === "complete" || !phase;
  const resolvedRequiredArtifact = phase ? path.resolve(resolvedRunDir, phase.artifact) : null;
  const requiredArtifact = resolvedRequiredArtifact;
  let status = complete ? "complete" : state.status;
  let reason = complete ? "workflow_complete" : "in_progress";
  if (!complete && resolvedRequiredArtifact && !fs.existsSync(resolvedRequiredArtifact)) {
    status = "awaiting_host";
    reason = "missing_phase_artifact";
  } else if (!complete && requiredArtifact) {
    status = "blocked";
    if (artifactIsStale(state, resolvedRequiredArtifact)) {
      reason = "stale_artifact";
    } else {
      const missing = missingMarkersForPhase(
        fs.readFileSync(resolvedRequiredArtifact, "utf8"),
        phase,
        root,
        localSkillContextForState(state, root),
      );
      if (missing.length > 0) {
        reason = "missing_completion_markers";
      } else if (phase.pause_after && !pauseAfterApprovalMatches(state, phase, resolvedRequiredArtifact)) {
        reason = pauseAfterPendingMatches(state, phase, resolvedRequiredArtifact)
          ? "pause_after"
          : "phase_artifact_written_advance_required";
      } else {
        try {
          nextPhaseIndex(state, phases, phase, resolvedRequiredArtifact);
          reason = "phase_artifact_written_advance_required";
        } catch (_error) {
          reason = "route_blocked";
        }
      }
    }
  }
  const nextCommand = complete
    ? "none"
    : reason === "pause_after"
      ? pauseApprovalCommand()
      : reason === "phase_artifact_written_advance_required"
        ? `${AGENT_FLOW_COMMAND} run advance`
        : `${AGENT_FLOW_COMMAND} run next`;
  return {
    status,
    run: `${state.workflow}/${state.run_id}`,
    task: state.task ?? "",
    current_phase: phase?.id ?? "-",
    reason,
    required_artifact: requiredArtifact,
    report: null,
    next_command: nextCommand,
    workspace_root: workspaceRoot,
  };
}

function printStatus(state, root, phases = null) {
  const payload = computeStatusPayload(state, root, phases);
  console.log(`${state.workflow} ${state.run_id} ${payload.status} phase=${payload.current_phase}`);
  console.log(`status: ${statusValue(payload.status)}`);
  console.log(`run: ${statusValue(payload.run)}`);
  console.log(`task: ${statusValue(payload.task)}`);
  console.log(`current_phase: ${statusValue(payload.current_phase)}`);
  printWorkspacePolicy(payload.workspace_root);
  console.log(`reason: ${statusValue(payload.reason)}`);
  if (payload.required_artifact) {
    console.log(`required_artifact: ${statusValue(payload.required_artifact)}`);
  }
  console.log(`next_command: ${statusValue(payload.next_command)}`);
  console.log(`status_json: ${JSON.stringify(payload)}`);
}

function artifactSha256(artifact) {
  return crypto.createHash("sha256").update(fs.readFileSync(artifact)).digest("hex");
}

function pauseAfterPendingMatches(state, phase, artifact) {
  const pending = state.pause_after_pending;
  return Boolean(
    pending
    && pending.phase === phase.id
    && pending.artifact_sha256 === artifactSha256(artifact),
  );
}

function pauseAfterApprovalMatches(state, phase, artifact) {
  if (!fs.existsSync(artifact)) return false;
  const approval = state.pause_after_approval;
  return Boolean(
    approval
    && approval.phase === phase.id
    && approval.artifact_sha256 === artifactSha256(artifact),
  );
}

function pauseApprovalCommand() {
  return `${AGENT_FLOW_COMMAND} run advance --approve-pause`;
}

function printPauseAfter(state, phase, root) {
  const requiredArtifact = path.resolve(resolveRunDir(root, state.run_dir), phase.artifact);
  const nextCommand = pauseApprovalCommand();
  const workspaceRoot = workspaceRootForState(state, root);
  const payload = {
    status: "blocked",
    run: `${state.workflow}/${state.run_id}`,
    task: state.task ?? "",
    current_phase: phase.id,
    reason: "pause_after",
    required_artifact: requiredArtifact,
    next_command: nextCommand,
    workspace_root: workspaceRoot,
  };
  console.log(`Pause after phase: ${phase.id}`);
  console.log("Review the artifact and wait for explicit user approval before running the next command.");
  console.log("status: blocked");
  console.log(`run: ${statusValue(payload.run)}`);
  console.log(`task: ${statusValue(payload.task)}`);
  console.log(`current_phase: ${statusValue(phase.id)}`);
  printWorkspacePolicy(workspaceRoot);
  console.log("reason: pause_after");
  console.log(`required_artifact: ${statusValue(requiredArtifact)}`);
  console.log(`next_command: ${statusValue(nextCommand)}`);
  console.log(`status_json: ${JSON.stringify(payload)}`);
}

function relevantLorePromptBlock(root, phase, state) {
  if (!phase.cite_lore || !String(state.task ?? "").trim()) return "";
  const lore = state.lore_citations || [];
  if (lore.length === 0) return "";
  const lines = [
    "## Relevant lore (auto-cited from `.agent-flow/memory/lore/`)",
    "",
    "These entries match the task keywords. Cite by relative path where they actually apply; ignore entries that aren't relevant. Do NOT fabricate citations.",
    "",
  ];
  for (const entry of lore) {
    const citationPath = String(entry.path);
    lines.push(`### \`${citationPath}\` (weight ${Number(entry.weight).toFixed(2)}, type ${entry.type})`);
    lines.push(`**Title**: ${entry.title}`);
    if (entry.constraint) lines.push(`**Constraint**: ${singleLine(entry.constraint, 200)}`);
    if (entry.directive) lines.push(`**Directive**: ${singleLine(entry.directive, 200)}`);
    lines.push("");
  }
  return `\n${lines.join("\n")}\n`;
}

function createLoreSnapshot(root, task) {
  const citations = searchRelevantLore(root, task).map((entry) => {
    const absolute = path.resolve(String(entry.path));
    const relative = path.relative(root, absolute).split(path.sep).join("/");
    if (!relative || relative.startsWith("../") || path.isAbsolute(relative)) {
      throw new Error(`blocked: lore citation is outside the project snapshot: ${entry.path}`);
    }
    return {
      path: relative,
      title: String(entry.title),
      type: String(entry.type),
      weight: Number(entry.weight),
      constraint: String(entry.constraint),
      directive: String(entry.directive),
    };
  });
  return {
    lore_snapshot_version: 1,
    lore_snapshot_hash: loreSnapshotHash(citations),
    lore_citations: citations,
  };
}

function assertLoreSnapshotPinned(state, root) {
  if (state.lore_snapshot_version == null && state.lore_snapshot_hash == null && state.lore_citations == null) {
    if (Number(state.phase_index ?? 0) !== 0) {
      throw new Error("blocked: an unpinned legacy run already emitted a phase prompt; its original lore citations cannot be reconstructed safely");
    }
    const migrated = { ...state, ...createLoreSnapshot(root, state.task ?? ""), lore_snapshot_migrated: true };
    writeJson(path.join(resolveRunDir(root, state.run_dir), "manifest.json"), migrated);
    writeJson(currentRunPath(root, migrated), migrated);
    return migrated;
  }
  if (state.lore_snapshot_version !== 1 || !Array.isArray(state.lore_citations)) {
    throw new Error("blocked: active run has an invalid lore snapshot");
  }
  if (state.lore_snapshot_hash !== loreSnapshotHash(state.lore_citations)) {
    throw new Error("blocked: active run lore snapshot changed; restore the run state or start a new run");
  }
  return state;
}

function loreSnapshotHash(citations) {
  const normalized = citations.map((entry) => ({
    constraint: String(entry.constraint),
    directive: String(entry.directive),
    path: String(entry.path),
    title: String(entry.title),
    type: String(entry.type),
    weight: Number(entry.weight),
  }));
  return crypto.createHash("sha256").update(JSON.stringify(normalized)).digest("hex");
}

function searchRelevantLore(root, task) {
  const keywords = String(task)
    .split(/[\s,/.:;()\[\]{}"'<>!?]+/u)
    .filter((token) => token.length >= 3 && !/^\d+$/u.test(token))
    .map((token) => token.toLowerCase());
  if (keywords.length === 0) return [];
  const loreRoot = path.join(root, ".agent-flow", "memory", "lore");
  const metadata = lstatIfExists(loreRoot);
  if (!metadata) return [];
  if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
    throw new Error(`blocked: unsafe lore directory: ${loreRoot}`);
  }
  const scored = [];
  for (const entry of safeDirectoryEntries(loreRoot, "lore directory")) {
    if (!entry.isFile() || entry.isSymbolicLink() || !entry.name.endsWith(".md")) continue;
    const file = path.join(loreRoot, entry.name);
    const lore = parseLoreForSearch(file);
    if (!lore) continue;
    const haystack = [
      lore.title,
      lore.scope,
      lore.tags.join(" "),
      lore.constraint,
      lore.rejected.join(" "),
      lore.directive,
    ].join(" ").toLowerCase();
    let score = 0;
    for (const keyword of keywords) {
      const wordMatches = haystack.match(new RegExp(`\\b${escapeRegex(keyword)}\\b`, "gu"))?.length ?? 0;
      const substringMatches = countNonOverlapping(haystack, keyword) - wordMatches;
      score += Math.min(wordMatches, 5) * 3 + Math.min(substringMatches, 5);
    }
    if (score > 0) scored.push({ score, lore });
  }
  scored.sort((left, right) => (
    right.score - left.score
    || right.lore.weight - left.lore.weight
    || compareCodePoints(path.basename(left.lore.path), path.basename(right.lore.path))
  ));
  return scored.slice(0, 5).map(({ lore }) => lore);
}

function parseLoreForSearch(file) {
  let text;
  try {
    text = fs.readFileSync(file, "utf8").replace(/^\uFEFF/u, "").replace(/\r\n?/gu, "\n");
  } catch {
    return null;
  }
  if (!text.startsWith("---\n")) return null;
  const end = text.indexOf("\n---\n", 4);
  if (end < 0) return null;
  let frontmatter;
  try {
    frontmatter = parseYamlMapping(text.slice(4, end), `lore frontmatter ${file}`);
  } catch {
    return null;
  }
  const body = text.slice(end + 5);
  const rawWeight = Number(frontmatter.weight ?? 1);
  const weight = Number.isFinite(rawWeight) ? rawWeight : 1;
  const tags = Array.isArray(frontmatter.tags)
    ? frontmatter.tags.map(String)
    : typeof frontmatter.tags === "string" ? [frontmatter.tags] : [];
  return {
    path: file,
    title: String(frontmatter.title ?? path.basename(file, ".md")),
    type: String(frontmatter.type ?? "decision"),
    scope: String(frontmatter.scope ?? ""),
    weight,
    tags,
    constraint: loreSection(body, "Constraint"),
    rejected: loreListSection(body, "Rejected"),
    directive: loreSection(body, "Directive"),
  };
}

function loreSection(body, name) {
  const match = body.match(new RegExp(`^##\\s+${escapeRegex(name)}\\s*\\n([\\s\\S]*?)(?=^##\\s+|(?![\\s\\S]))`, "mu"));
  return match?.[1]?.trim() ?? "";
}

function loreListSection(body, name) {
  return [...loreSection(body, name).matchAll(/^[-*]\s+(.+?)$/gmu)].map((match) => match[1].trim());
}

function countNonOverlapping(text, needle) {
  let count = 0;
  let offset = 0;
  while (needle && (offset = text.indexOf(needle, offset)) >= 0) {
    count += 1;
    offset += needle.length;
  }
  return count;
}

function singleLine(value, limit) {
  const normalized = String(value).replace(/\s+/g, " ").trim();
  return normalized.length <= limit ? normalized : `${normalized.slice(0, limit - 1)}…`;
}

function statusValue(value) {
  return String(value).replace(/\r/g, "\\r").replace(/\n/g, "\\n");
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
  const current = lstatIfExists(pathName);
  if (current && (current.isSymbolicLink() || !current.isFile())) {
    throw new Error(`blocked: JSON destination is unsafe: ${pathName}`);
  }
  const mode = current ? current.mode & 0o777 : (0o666 & ~process.umask());
  const transactionWrites = pendingProjectSkillHostTransaction?.persistent?.transactionRoot
    ? path.join(pendingProjectSkillHostTransaction.persistent.transactionRoot, "writes")
    : path.dirname(pathName);
  const temporary = path.join(
    transactionWrites,
    `.${path.basename(pathName)}.agent-flow-${process.pid}-${crypto.randomBytes(6).toString("hex")}.tmp`,
  );
  let descriptor;
  try {
    descriptor = fs.openSync(temporary, "wx", 0o600);
    fs.fchmodSync(descriptor, mode);
    fs.writeFileSync(descriptor, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
    fs.fsyncSync(descriptor);
    fs.closeSync(descriptor);
    descriptor = undefined;
    const journalPath = pendingProjectSkillHostTransaction?.persistent?.journalPath;
    let prepared = [];
    if (!journalPath || path.resolve(pathName) !== path.resolve(journalPath)) {
      prepared = preparePendingCriticalInstallMutation(pathName, temporary);
    }
    assertPreparedCriticalInstallMutationUnchanged(prepared, pathName);
    fs.renameSync(temporary, pathName);
    if (!journalPath || path.resolve(pathName) !== path.resolve(journalPath)) {
      checkpointPendingCriticalInstall(pathName);
    }
  } catch (error) {
    if (descriptor !== undefined) fs.closeSync(descriptor);
    fs.rmSync(temporary, { force: true });
    throw error;
  }
}

function fsyncDirectoryPath(directory) {
  const descriptor = fs.openSync(directory, "r");
  try {
    fs.fsyncSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
}

function writeInstallTransactionJournal(journalPath, journal) {
  writeJson(journalPath, journal);
  fsyncDirectoryPath(path.dirname(journalPath));
}

function readExistingKit(agentFlowDir) {
  const kitPath = path.join(agentFlowDir, "kit.json");
  const metadata = lstatIfExists(kitPath);
  if (!metadata) return undefined;
  if (metadata.isSymbolicLink() || !metadata.isFile()) {
    throw new Error(`blocked: installed kit metadata must be a regular file: ${kitPath}`);
  }
  try {
    const payload = JSON.parse(fs.readFileSync(kitPath, "utf8"));
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error("metadata is not a JSON object");
    }
    return payload;
  } catch (error) {
    throw new Error(
      `blocked: installed kit metadata is invalid: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
}

function readExistingSkillIndex(agentFlowDir) {
  const indexPath = path.join(agentFlowDir, "skills", "index.json");
  const metadata = lstatIfExists(indexPath);
  if (!metadata) return null;
  if (metadata.isSymbolicLink() || !metadata.isFile()) {
    throw new Error(`blocked: previous skill index must be a regular file: ${indexPath}`);
  }
  try {
    const payload = JSON.parse(fs.readFileSync(indexPath, "utf8"));
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error("metadata is not a JSON object");
    }
    return payload;
  } catch (error) {
    throw new Error(
      `blocked: previous skill index is invalid: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
}

function authenticatedPreviousSkillIndex(
  root,
  existingPayload,
  previousIndex,
  installSelection,
  { legacyMigration = false } = {},
) {
  if (!existingPayload) {
    if (previousIndex) {
      throw new Error("blocked: previous skill index exists without kit provenance");
    }
    return null;
  }
  const hasVersion = Object.hasOwn(existingPayload, "skill_plan_hash_version");
  const hasHash = Object.hasOwn(existingPayload, "skill_plan_hash");
  if (!hasVersion && !hasHash) {
    if (!installSelection?.explicitProfileSelection) {
      throw new Error("blocked: legacy install migration requires an explicit --profile");
    }
    const previousExplicitSkills = previousIndex?.selection?.explicit_skills ?? [];
    if (
      !Array.isArray(previousExplicitSkills)
      || previousExplicitSkills.some((name) => typeof name !== "string" || !name)
    ) {
      throw new Error("blocked: legacy skill index has invalid explicit skill metadata");
    }
    const requested = new Set(installSelection.explicitSkills ?? []);
    const missing = previousExplicitSkills.filter((name) => !requested.has(name));
    if (missing.length > 0) {
      throw new Error(
        `blocked: legacy install migration must explicitly retain skills: ${missing.join(", ")}`,
      );
    }
    if (legacyMigration) {
      return authenticateSupportedLegacySkillIndex(root, previousIndex);
    }
    return null;
  }
  if (
    existingPayload.skill_plan_hash_version !== 2
    || typeof existingPayload.skill_plan_hash !== "string"
    || !/^[0-9a-f]{64}$/.test(existingPayload.skill_plan_hash)
  ) {
    throw new Error("blocked: previous skill index has invalid kit provenance");
  }
  if (!previousIndex) {
    throw new Error("blocked: previous skill index required by kit provenance is missing");
  }
  let computed;
  try {
    computed = computeSkillPlanHash(previousIndex, root, false);
  } catch (error) {
    throw new Error(
      `blocked: previous skill index provenance is invalid: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  if (computed !== existingPayload.skill_plan_hash) {
    throw new Error("blocked: previous skill index does not match kit provenance");
  }
  return {
    ...previousIndex,
    links: authenticatedPreviousSkillLinks(existingPayload, previousIndex),
  };
}

function authenticateSupportedLegacySkillIndex(root, previousIndex) {
  if (
    !previousIndex
    || previousIndex.version !== 1
    || !Array.isArray(previousIndex.skills)
    || previousIndex.skills.length === 0
  ) {
    throw new Error("blocked: supported legacy migration requires its complete skill index");
  }
  const seen = new Set();
  const rows = [];
  const skills = previousIndex.skills.map((skill) => {
    const name = skill?.name;
    if (
      !isPortableSkillName(name)
      || skill.source !== "bundled"
      || skill.path !== `.agent-flow/skills/${name}/SKILL.md`
    ) {
      throw new Error("blocked: legacy skill index does not match the supported migration inventory");
    }
    const logicalName = portableSkillCasefold(name);
    if (seen.has(logicalName)) {
      throw new Error("blocked: legacy skill index does not match the supported migration inventory");
    }
    seen.add(logicalName);
    const skillRoot = path.join(root, ".agent-flow", "skills", name);
    const treeHash = hashSkillTree(skillRoot);
    rows.push({ name, tree_hash: treeHash });
    return {
      ...skill,
      source: "bundled",
      source_host: null,
      tree_hash: treeHash,
    };
  });
  rows.sort((left, right) => compareCodePoints(left.name, right.name));
  const commitment = sha256Bytes(Buffer.from(JSON.stringify({
    version: 1,
    base_commit: LEGACY_MIGRATION_BASE_COMMIT,
    skills: rows,
  }), "utf8"));
  if (commitment !== LEGACY_SKILL_TREE_COMMITMENT) {
    throw new Error("blocked: legacy skill snapshots changed from the supported migration base");
  }
  return { ...previousIndex, skills, links: [] };
}

function authenticatedPreviousSkillLinks(existingPayload, previousIndex) {
  const hasVersion = Object.hasOwn(existingPayload, "skill_links_commitment_version");
  const hasCommitment = Object.hasOwn(existingPayload, "skill_links_commitment");
  if (!hasVersion && !hasCommitment) {
    return [];
  }
  if (
    !hasVersion
    || !hasCommitment
    || existingPayload.skill_links_commitment_version !== SKILL_LINKS_COMMITMENT_VERSION
    || typeof existingPayload.skill_links_commitment !== "string"
    || !/^[0-9a-f]{64}$/.test(existingPayload.skill_links_commitment)
  ) {
    throw new Error("blocked: previous skill link commitment is invalid");
  }
  const computed = skillLinksCommitment(existingPayload.skill_plan_hash, previousIndex.links);
  if (computed !== existingPayload.skill_links_commitment) {
    throw new Error("blocked: previous skill links do not match kit commitment");
  }
  return previousIndex.links;
}

function skillLinksCommitment(skillPlanHash, links) {
  if (typeof skillPlanHash !== "string" || !/^[0-9a-f]{64}$/.test(skillPlanHash)) {
    throw new Error("blocked: skill link commitment has an invalid skill plan hash");
  }
  return sha256Bytes(Buffer.from(JSON.stringify({
    version: SKILL_LINKS_COMMITMENT_VERSION,
    skill_plan_hash: skillPlanHash,
    links: normalizedSkillLinksForCommitment(links),
  }), "utf8"));
}

function normalizedSkillLinksForCommitment(links) {
  if (!Array.isArray(links)) {
    throw new Error("blocked: installed skill links are invalid");
  }
  const allowedStatuses = new Set([
    "linked",
    "copied",
    "removed-stale-linked",
    "removed-stale-copied",
  ]);
  const allowedHosts = new Set([...PROJECT_SKILL_HOSTS, "gemini", "antigravity"]);
  const seen = new Set();
  const rows = links.map((link) => {
    if (!link || typeof link !== "object" || Array.isArray(link)) {
      throw new Error("blocked: installed skill link is invalid");
    }
    const name = typeof link.name === "string" ? link.name : "";
    const host = typeof link.host === "string" ? link.host : "";
    const relative = typeof link.path === "string" ? link.path : "";
    const status = typeof link.status === "string" ? link.status : "";
    const integrity = link.tree_integrity ?? null;
    if (
      !name
      || !isPortableSkillName(name)
      || name.length > 128
      || !allowedHosts.has(host)
      || !allowedStatuses.has(status)
      || relative.includes("\\")
      || relative.startsWith("/")
      || relative.split("/").some((part) => !part || part === "." || part === "..")
      || (integrity !== null && (
        typeof integrity !== "string"
        || !/^[0-9a-f]{64}$/.test(integrity)
      ))
    ) {
      throw new Error(`blocked: installed skill link is invalid: ${name || "unknown"}`);
    }
    const expectedPaths = new Set(
      host === "codex"
        ? [
            path.posix.join(".agents", "skills", name),
            path.posix.join(".Codex", "skills", name),
            path.posix.join(".codex", "skills", name),
          ]
        : host === "antigravity"
          ? [path.posix.join(".gemini", "antigravity", "skills", name)]
          : [path.posix.join(`.${host}`, "skills", name)],
    );
    if (!expectedPaths.has(relative)) {
      throw new Error(`blocked: installed skill link path is noncanonical: ${relative}`);
    }
    const identity = `${host}\u0000${name}\u0000${relative}`;
    if (seen.has(identity)) {
      throw new Error(`blocked: duplicate installed skill link: ${host}:${name}`);
    }
    seen.add(identity);
    return [name, host, relative, status, integrity];
  });
  return rows.sort((left, right) => compareCodePoints(JSON.stringify(left), JSON.stringify(right)));
}

function writeFileIfMissing(pathName, content) {
  fs.mkdirSync(path.dirname(pathName), { recursive: true });
  if (!fs.existsSync(pathName)) {
    fs.writeFileSync(pathName, content, "utf8");
  }
}

function writeManagedFile(pathName, content) {
  atomicInstallWrite(pathName, content);
}

function writeManagedSkill(pathName, content) {
  const lineCount = String(content).split(/\r?\n/).length;
  if (lineCount > 200) {
    throw new Error(`${pathName}: ${lineCount} lines; max is 200, split progressive references`);
  }
  writeManagedFile(pathName, content);
}

function writeManagedFileIfMissingOrSame(pathName, content, force = false) {
  const installTransaction = pendingProjectSkillHostTransaction?.persistent;
  if (installTransaction) ensureInstallDirectoryWithProgress(installTransaction, path.dirname(pathName));
  else fs.mkdirSync(path.dirname(pathName), { recursive: true });
  const next = Buffer.isBuffer(content) ? content : Buffer.from(String(content), "utf8");
  if (fs.existsSync(pathName)) {
    const current = fs.readFileSync(pathName);
    if (force) {
      atomicInstallWrite(pathName, next);
      return true;
    }
    if (!current.equals(next)) {
      return false;
    }
    return true;
  }
  atomicInstallWrite(pathName, next);
  return true;
}

function atomicInstallWrite(pathName, content) {
  const installTransaction = pendingProjectSkillHostTransaction?.persistent;
  if (installTransaction) ensureInstallDirectoryWithProgress(installTransaction, path.dirname(pathName));
  else fs.mkdirSync(path.dirname(pathName), { recursive: true });
  const current = lstatIfExists(pathName);
  if (current && (current.isSymbolicLink() || !current.isFile())) {
    throw new Error(`blocked: managed install file is unsafe: ${pathName}`);
  }
  const mode = current ? current.mode & 0o777 : (0o666 & ~process.umask());
  const transactionWrites = pendingProjectSkillHostTransaction?.persistent?.transactionRoot
    ? path.join(pendingProjectSkillHostTransaction.persistent.transactionRoot, "writes")
    : path.dirname(pathName);
  const temporary = path.join(
    transactionWrites,
    `.${path.basename(pathName)}.agent-flow-${process.pid}-${crypto.randomBytes(6).toString("hex")}.tmp`,
  );
  let descriptor;
  try {
    descriptor = fs.openSync(temporary, "wx", 0o600);
    fs.fchmodSync(descriptor, mode);
    fs.writeFileSync(descriptor, content);
    fs.fsyncSync(descriptor);
    fs.closeSync(descriptor);
    descriptor = undefined;
    const prepared = preparePendingCriticalInstallMutation(pathName, temporary);
    assertPreparedCriticalInstallMutationUnchanged(prepared, pathName);
    fs.renameSync(temporary, pathName);
  } catch (error) {
    if (descriptor !== undefined) fs.closeSync(descriptor);
    fs.rmSync(temporary, { force: true });
    throw error;
  }
  checkpointPendingCriticalInstall(pathName);
}

function checkpointPendingCriticalInstall(candidate) {
  const persistent = pendingProjectSkillHostTransaction?.persistent;
  if (!persistent || persistent.journal.status !== "open") return;
  let changed = false;
  for (let index = 0; index < persistent.snapshots.length; index += 1) {
    const snapshot = persistent.snapshots[index];
    if (
      !installTransactionPathsAlias(snapshot.file, candidate)
      && !(snapshot.kind === "directory" && installTransactionPathContains(snapshot.file, candidate))
    ) {
      continue;
    }
    const journalEntry = persistent.journal.files[index];
    const currentState = installTransactionPathState(
      snapshot.file,
      snapshot.kind,
      "critical install mutation checkpoint",
    );
    if (
      journalEntry.pending_state === null
      || !installTransactionStatesEqual(
        currentState,
        validateInstallTransactionState(journalEntry.pending_state, snapshot.kind, true),
      )
    ) {
      throw new Error(`blocked: critical install mutation did not match write-ahead state: ${snapshot.relative}`);
    }
    journalEntry.applied_state = currentState;
    journalEntry.pending_state = null;
    changed = true;
  }
  if (changed) writeInstallTransactionJournal(persistent.journalPath, persistent.journal);
}

function preparePendingCriticalInstallMutation(candidate, replacement = null) {
  const persistent = pendingProjectSkillHostTransaction?.persistent;
  if (!persistent || persistent.journal.status !== "open") return [];
  const observations = [];
  for (let index = 0; index < persistent.snapshots.length; index += 1) {
    const snapshot = persistent.snapshots[index];
    const exact = installTransactionPathsAlias(snapshot.file, candidate);
    if (!exact && !(snapshot.kind === "directory" && installTransactionPathContains(snapshot.file, candidate))) {
      continue;
    }
    const journalEntry = persistent.journal.files[index];
    if (journalEntry.pending_state !== null) {
      throw new Error(`blocked: critical install mutation is already pending: ${snapshot.relative}`);
    }
    const appliedState = validateInstallTransactionState(journalEntry.applied_state, snapshot.kind, true);
    const liveState = installTransactionPathState(
      snapshot.file,
      snapshot.kind,
      "critical install pre-mutation state",
    );
    if (!installTransactionStatesEqual(liveState, appliedState)) {
      throw new Error(`blocked: critical install destination changed during install: ${snapshot.relative}`);
    }
    observations.push({
      file: snapshot.file,
      kind: snapshot.kind,
      relative: snapshot.relative,
      state: liveState,
    });
    let pendingState;
    if (exact) {
      pendingState = replacement === null
        ? { kind: "absent" }
        : installTransactionPathState(replacement, snapshot.kind, "critical install replacement");
    } else {
      const rootMetadata = fs.lstatSync(snapshot.file);
      const rootMode = rootMetadata.mode & 0o777;
      const relative = path.relative(snapshot.file, candidate).split(path.sep).join("/");
      const prefix = `${relative}/`;
      const entries = installTransactionTreeEntries(snapshot.file).filter(
        (entry) => entry.path !== relative && !entry.path.startsWith(prefix),
      );
      if (replacement !== null) {
        const replacementMetadata = fs.lstatSync(replacement);
        if (replacementMetadata.isSymbolicLink()) {
          throw new Error(`blocked: critical install replacement may not be a symlink: ${replacement}`);
        }
        if (replacementMetadata.isFile()) {
          entries.push({
            path: relative,
            type: "file",
            mode: replacementMetadata.mode & 0o777,
            sha256: crypto.createHash("sha256").update(fs.readFileSync(replacement)).digest("hex"),
          });
        } else if (replacementMetadata.isDirectory()) {
          for (const entry of installTransactionTreeEntries(replacement)) {
            entries.push({
              ...entry,
              path: entry.path ? `${relative}/${entry.path}` : relative,
            });
          }
        } else {
          throw new Error(`blocked: critical install replacement may not be a special file: ${replacement}`);
        }
      }
      entries.sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
      pendingState = {
        kind: "directory",
        mode: rootMode,
        tree_hash: installTransactionTreeIntegrityFromEntries(entries),
      };
    }
    journalEntry.pending_state = pendingState;
    writeInstallTransactionJournal(persistent.journalPath, persistent.journal);
  }
  return observations;
}

function assertPreparedCriticalInstallMutationUnchanged(observations, candidate) {
  if (!Array.isArray(observations) || observations.length === 0) return;
  const holdMs = Number.parseInt(
    process.env.AGENT_FLOW_TEST_HOLD_AFTER_CRITICAL_PREPARE_MS ?? "0",
    10,
  );
  const holdSuffix = process.env.AGENT_FLOW_TEST_CRITICAL_PREPARE_SUFFIX;
  if (
    typeof holdSuffix === "string"
    && holdSuffix
    && String(candidate).endsWith(holdSuffix)
    && Number.isInteger(holdMs)
    && holdMs > 0
    && holdMs <= 10_000
  ) {
    const transactionRoot = pendingProjectSkillHostTransaction?.persistent?.transactionRoot;
    if (transactionRoot) {
      fs.writeFileSync(path.join(transactionRoot, "critical-rename-ready"), "ready\n", "utf8");
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, holdMs);
    }
  }
  for (const observation of observations) {
    const current = installTransactionPathState(
      observation.file,
      observation.kind,
      "critical install immediate pre-mutation state",
    );
    if (!installTransactionStatesEqual(current, observation.state)) {
      throw new Error(
        `blocked: critical install destination changed before mutation: ${observation.relative}`,
      );
    }
  }
}

function installTransactionPathsAlias(left, right) {
  if (path.resolve(left) === path.resolve(right)) return true;
  try {
    return fs.realpathSync.native(left) === fs.realpathSync.native(right);
  } catch {
    try {
      return fs.realpathSync.native(path.dirname(left)) === fs.realpathSync.native(path.dirname(right))
        && path.basename(left) === path.basename(right);
    } catch {
      return false;
    }
  }
}

function sha256Bytes(content) {
  return crypto.createHash("sha256").update(content).digest("hex");
}

function managedHostRelativePath(parts) {
  return parts.filter(Boolean).join("/");
}

function isManagedHostRelativePath(relative) {
  if (relative === ".omp/extensions/agent-flow-hooks.ts") {
    return true;
  }
  return [".Codex/agents/", ".claude/agents/", ".omp/agents/"]
    .some((prefix) => relative.startsWith(prefix) && relative.length > prefix.length);
}

function validateManagedHostRelativePath(relative) {
  if (
    typeof relative !== "string"
    || relative.includes("\\")
    || relative.startsWith("/")
    || relative.split("/").some((part) => !part || part === "." || part === "..")
    || !isManagedHostRelativePath(relative)
  ) {
    throw new Error(`blocked: invalid managed host file provenance path: ${JSON.stringify(relative)}`);
  }
  return relative;
}

function collectManagedHostSourceFiles(sourceRoot, destinationRoot, sourceLabelRoot) {
  const rootStat = lstatIfExists(sourceRoot);
  if (!rootStat || rootStat.isSymbolicLink() || !rootStat.isDirectory()) {
    throw new Error(`blocked: managed host source directory is invalid: ${sourceRoot}`);
  }
  const files = [];
  const visit = (directory, parts) => {
    const entries = fs.readdirSync(directory, { withFileTypes: true })
      .sort((left, right) => compareCodePoints(left.name, right.name));
    for (const entry of entries) {
      const source = path.join(directory, entry.name);
      const nextParts = [...parts, entry.name];
      if (entry.isSymbolicLink()) {
        throw new Error(`blocked: managed host source may not use symlinks: ${source}`);
      }
      if (entry.isDirectory()) {
        visit(source, nextParts);
        continue;
      }
      if (!entry.isFile()) {
        throw new Error(`blocked: managed host source must contain regular files: ${source}`);
      }
      if (fs.lstatSync(source).nlink !== 1) {
        throw new Error(`blocked: managed host source may not use hard-linked files: ${source}`);
      }
      const relative = managedHostRelativePath([destinationRoot, ...nextParts]);
      files.push({
        relative: validateManagedHostRelativePath(relative),
        source: managedHostRelativePath([sourceLabelRoot, ...nextParts]),
        content: fs.readFileSync(source),
      });
    }
  };
  visit(sourceRoot, []);
  return files;
}

function managedReviewerBody(content) {
  const text = content.toString("utf8");
  if (!text.startsWith("---\n")) {
    return text;
  }
  const end = text.indexOf("\n---\n", 4);
  return end === -1 ? text : text.slice(end + "\n---\n".length).replace(/^\n/, "");
}

function assertManagedReviewerParity(specs) {
  const byPath = new Map(specs.map((spec) => [spec.relative, spec.content]));
  const codex = byPath.get(".Codex/agents/code-reviewer.md");
  const claude = byPath.get(".claude/agents/code-reviewer.md");
  const omp = byPath.get(".omp/agents/code-reviewer.md");
  if (!codex || !claude || !omp) {
    throw new Error("blocked: managed host reviewer sources are incomplete");
  }
  if (managedReviewerBody(codex) !== managedReviewerBody(claude) || !omp.equals(claude)) {
    throw new Error("blocked: Claude, Codex, and OMP managed reviewers are not equivalent");
  }
}

function desiredManagedHostFiles(root) {
  const specs = [
    ...collectManagedHostSourceFiles(
      path.join(KIT_ROOT, ".Codex", "agents"),
      ".Codex/agents",
      ".Codex/agents",
    ),
    ...collectManagedHostSourceFiles(
      path.join(KIT_ROOT, ".claude", "agents"),
      ".claude/agents",
      ".claude/agents",
    ),
    ...collectManagedHostSourceFiles(
      path.join(KIT_ROOT, ".claude", "agents"),
      ".omp/agents",
      ".claude/agents",
    ),
    {
      relative: ".omp/extensions/agent-flow-hooks.ts",
      source: "generated:omp-hooks-extension",
      content: Buffer.from(ompHooksExtensionSource(root), "utf8"),
    },
  ].sort((left, right) => compareCodePoints(left.relative, right.relative));
  const seen = new Set();
  for (const spec of specs) {
    if (seen.has(spec.relative)) {
      throw new Error(`blocked: duplicate managed host destination: ${spec.relative}`);
    }
    seen.add(spec.relative);
    spec.destination = path.join(root, ...spec.relative.split("/"));
    spec.sha256 = sha256Bytes(spec.content);
  }
  for (const relative of REQUIRED_MANAGED_HOST_FILES) {
    if (!seen.has(relative)) {
      throw new Error(`blocked: managed host source is missing required file: ${relative}`);
    }
  }
  assertManagedReviewerParity(specs);
  return specs;
}

function readManagedHostFileProvenance(payload, { required = false } = {}) {
  const manifest = payload?.managed_host_files;
  if (manifest === undefined && !required) {
    return new Map();
  }
  if (
    !manifest
    || typeof manifest !== "object"
    || Array.isArray(manifest)
    || manifest.version !== MANAGED_HOST_FILES_VERSION
    || !manifest.files
    || typeof manifest.files !== "object"
    || Array.isArray(manifest.files)
  ) {
    throw new Error("blocked: installed managed host file provenance is invalid");
  }
  const result = new Map();
  for (const [rawRelative, entry] of Object.entries(manifest.files)) {
    const relative = validateManagedHostRelativePath(rawRelative);
    if (
      !entry
      || typeof entry !== "object"
      || Array.isArray(entry)
      || typeof entry.source !== "string"
      || !entry.source.trim()
      || typeof entry.sha256 !== "string"
      || !/^[0-9a-f]{64}$/.test(entry.sha256)
    ) {
      throw new Error(`blocked: installed managed host file provenance is invalid: ${relative}`);
    }
    result.set(relative, { source: entry.source, sha256: entry.sha256 });
  }
  return result;
}

function managedHostFilesCommitment(skillPlanHash, manifest) {
  if (typeof skillPlanHash !== "string" || !/^[0-9a-f]{64}$/.test(skillPlanHash)) {
    throw new Error("blocked: managed host file commitment has an invalid skill plan hash");
  }
  const provenance = readManagedHostFileProvenance(
    { managed_host_files: manifest },
    { required: true },
  );
  const files = [...provenance.entries()]
    .sort(([left], [right]) => compareCodePoints(left, right))
    .map(([relative, entry]) => [relative, entry.source, entry.sha256]);
  return sha256Bytes(Buffer.from(JSON.stringify({
    version: MANAGED_HOST_FILES_COMMITMENT_VERSION,
    skill_plan_hash: skillPlanHash,
    files,
  }), "utf8"));
}

function assertManagedHostFilesCommitment(payload, { required = false } = {}) {
  const hasVersion = Object.hasOwn(payload ?? {}, "managed_host_files_commitment_version");
  const hasCommitment = Object.hasOwn(payload ?? {}, "managed_host_files_commitment");
  if (!hasVersion && !hasCommitment && !required) return false;
  if (
    !hasVersion
    || !hasCommitment
    || payload.managed_host_files_commitment_version !== MANAGED_HOST_FILES_COMMITMENT_VERSION
    || typeof payload.managed_host_files_commitment !== "string"
    || !/^[0-9a-f]{64}$/.test(payload.managed_host_files_commitment)
  ) {
    throw new Error("blocked: installed managed host file commitment is invalid");
  }
  const computed = managedHostFilesCommitment(
    payload.skill_plan_hash,
    payload.managed_host_files,
  );
  if (computed !== payload.managed_host_files_commitment) {
    throw new Error("blocked: installed managed host file commitment does not match provenance");
  }
  return true;
}

function expectedManagedHookProjection() {
  return [
    ["PostToolUse", WRITE_TOOL_MATCHER, "command", "comment-checker.py"],
    ["PreToolUse", "Bash", "command", "guard-protected-branch.sh"],
    ["PreToolUse", "Bash", "command", "guard-worktree-write.py"],
    ["PreToolUse", "Bash", "command", "guard-worktree.sh"],
    ["PreToolUse", WRITE_TOOL_MATCHER, "command", "guard-worktree-write.py"],
    ["Stop", "", "command", "show-phase-status.sh"],
  ].sort(compareHookProjectionRows);
}

function compareHookProjectionRows(left, right) {
  for (let index = 0; index < Math.min(left.length, right.length); index += 1) {
    const compared = compareCodePoints(left[index], right[index]);
    if (compared !== 0) return compared;
  }
  return left.length - right.length;
}

function managedHookProjection(root, settings, label, expectedScriptHashes = null) {
  if (!settings || typeof settings !== "object" || Array.isArray(settings)) {
    throw new Error(`blocked: invalid managed hook settings: ${label}`);
  }
  const hooks = settings.hooks;
  if (!hooks || typeof hooks !== "object" || Array.isArray(hooks)) {
    throw new Error(`blocked: managed hook settings are missing: ${label}`);
  }
  const rows = [];
  for (const [event, entries] of Object.entries(hooks)) {
    if (!Array.isArray(entries)) {
      throw new Error(`blocked: invalid managed hook settings: ${label}`);
    }
    for (const entry of entries) {
      if (!entry || typeof entry !== "object" || Array.isArray(entry) || !Array.isArray(entry.hooks)) {
        throw new Error(`blocked: invalid managed hook settings: ${label}`);
      }
      const matcher = typeof entry.matcher === "string" ? entry.matcher : "";
      for (const hook of entry.hooks) {
        if (!hook || typeof hook !== "object" || Array.isArray(hook)) {
          throw new Error(`blocked: invalid managed hook settings: ${label}`);
        }
        const scriptName = trustedManagedHookScriptName(
          root,
          hook.command,
          expectedScriptHashes,
        );
        if (scriptName) {
          rows.push([event, matcher, hook.type ?? "", scriptName]);
        } else if (managedHookScriptName(hook.command)) {
          throw new Error(`blocked: managed hook command is not immutable: ${label}`);
        }
      }
    }
  }
  return rows.sort(compareHookProjectionRows);
}

function managedHookProjectionBytes(rows) {
  return Buffer.from(JSON.stringify(rows), "utf8");
}

function readManagedHookSettingsStrict(root, relative) {
  const file = path.join(root, ...relative.split("/"));
  requireInstalledRegularFile(root, file, `managed hook settings ${relative}`);
  let settings;
  try {
    settings = JSON.parse(fs.readFileSync(file, "utf8"));
  } catch (error) {
    throw new Error(`blocked: managed hook settings are unreadable: ${relative}: ${error.message}`);
  }
  return settings;
}

function preflightManagedHookScripts(root, existingPayload, { legacyMigration = false } = {}) {
  let authenticatedExisting = false;
  const entries = MANAGED_HOOK_SCRIPT_NAMES.map((scriptName) => {
    const relative = `.agent-flow/scripts/hooks/${scriptName}`;
    const source = path.join(KIT_ROOT, "scripts", "hooks", scriptName);
    const sourceMetadata = lstatIfExists(source);
    if (
      !sourceMetadata
      || sourceMetadata.isSymbolicLink()
      || !sourceMetadata.isFile()
      || sourceMetadata.nlink !== 1
    ) {
      throw new Error(`blocked: bundled managed hook script is unsafe: ${source}`);
    }
    const content = fs.readFileSync(source);
    const sha256 = sha256Bytes(content);
    const current = inspectManagedHookScriptDestination(root, relative);
    if (current.exists && current.sha256 !== sha256) {
      if (legacyMigration) {
        if (LEGACY_MANAGED_HOOK_HASHES[relative] !== current.sha256) {
          throw new Error(`blocked: user-modified legacy managed hook script differs: ${relative}`);
        }
      } else {
        if (!existingPayload) {
          throw new Error(`blocked: unauthenticated managed hook script differs: ${relative}`);
        }
        if (!authenticatedExisting) {
          assertManagedHookContractInstalled(root, existingPayload);
          authenticatedExisting = true;
        }
      }
    }
    return {
      relative,
      destination: path.join(root, ...relative.split("/")),
      content,
      sha256,
      expectedCurrentHash: current.sha256,
      expectedCurrentMode: current.mode,
    };
  });
  return { root: path.resolve(root), entries };
}

function applyManagedHookScriptPlan(plan) {
  for (const entry of plan.entries) {
    const current = inspectManagedHookScriptDestination(plan.root, entry.relative);
    if (
      current.sha256 !== entry.expectedCurrentHash
      || current.mode !== entry.expectedCurrentMode
    ) {
      throw new Error(`blocked: managed hook script changed during install: ${entry.relative}`);
    }
    if (current.sha256 !== entry.sha256) {
      atomicInstallWrite(entry.destination, entry.content);
    }
  }
}

function inspectManagedHookScriptDestination(root, relative) {
  const expected = new Set(
    MANAGED_HOOK_SCRIPT_NAMES.map((name) => `.agent-flow/scripts/hooks/${name}`),
  );
  if (!expected.has(relative)) {
    throw new Error(`blocked: invalid managed hook script path: ${relative}`);
  }
  let cursor = path.resolve(root);
  const parts = relative.split("/");
  for (let index = 0; index < parts.length; index += 1) {
    cursor = path.join(cursor, parts[index]);
    const metadata = lstatIfExists(cursor);
    if (!metadata) return { exists: false, sha256: null, mode: null };
    if (metadata.isSymbolicLink()) {
      throw new Error(`blocked: managed hook script path may not use symlinks: ${relative}`);
    }
    const final = index === parts.length - 1;
    if ((final && !metadata.isFile()) || (!final && !metadata.isDirectory())) {
      throw new Error(`blocked: managed hook script path has an invalid component: ${relative}`);
    }
  }
  const metadata = fs.statSync(cursor);
  if (metadata.nlink !== 1) {
    throw new Error(`blocked: managed hook script may not be hard-linked: ${relative}`);
  }
  return {
    exists: true,
    sha256: sha256Bytes(fs.readFileSync(cursor)),
    mode: metadata.mode & 0o777,
  };
}

function buildManagedHookContract(root) {
  const expected = expectedManagedHookProjection();
  const scripts = {};
  for (const scriptName of MANAGED_HOOK_SCRIPT_NAMES) {
    const relative = `.agent-flow/scripts/hooks/${scriptName}`;
    const file = path.join(root, ...relative.split("/"));
    requireInstalledRegularFile(root, file, `managed hook script ${relative}`);
    requireManagedHookScriptExecutable(file, relative);
    const source = path.join(KIT_ROOT, "scripts", "hooks", scriptName);
    const sourceMetadata = lstatIfExists(source);
    if (
      !sourceMetadata
      || sourceMetadata.isSymbolicLink()
      || !sourceMetadata.isFile()
      || !fs.readFileSync(file).equals(fs.readFileSync(source))
    ) {
      throw new Error(`blocked: managed hook script does not match the authenticated bundle: ${relative}`);
    }
    scripts[relative] = {
      sha256: sha256Bytes(fs.readFileSync(file)),
      mode: "executable",
    };
  }
  const expectedScriptHashes = new Map(
    Object.entries(scripts).map(([relative, entry]) => [relative, entry.sha256]),
  );
  const configs = {};
  for (const relative of MANAGED_HOOK_CONFIG_PATHS) {
    const projection = managedHookProjection(
      root,
      readManagedHookSettingsStrict(root, relative),
      relative,
      expectedScriptHashes,
    );
    if (JSON.stringify(projection) !== JSON.stringify(expected)) {
      throw new Error(`blocked: managed hook settings do not match the required contract: ${relative}`);
    }
    configs[relative] = { sha256: sha256Bytes(managedHookProjectionBytes(projection)) };
  }
  return { version: MANAGED_HOOK_CONTRACT_VERSION, configs, scripts };
}

function requireManagedHookScriptExecutable(file, relative) {
  if (process.platform === "win32") return;
  if ((fs.statSync(file).mode & 0o111) === 0) {
    throw new Error(`blocked: managed hook script is not executable: ${relative}`);
  }
}

function normalizedManagedHookContract(contract) {
  if (
    !contract
    || typeof contract !== "object"
    || Array.isArray(contract)
    || contract.version !== MANAGED_HOOK_CONTRACT_VERSION
  ) {
    throw new Error("blocked: installed managed hook contract is invalid");
  }
  const normalize = (entries, expectedPaths, label, requiredMode = undefined) => {
    if (!entries || typeof entries !== "object" || Array.isArray(entries)) {
      throw new Error(`blocked: installed managed hook ${label} provenance is invalid`);
    }
    const actualPaths = Object.keys(entries).sort(compareCodePoints);
    const requiredPaths = [...expectedPaths].sort(compareCodePoints);
    if (JSON.stringify(actualPaths) !== JSON.stringify(requiredPaths)) {
      throw new Error(`blocked: installed managed hook ${label} provenance is incomplete`);
    }
    return actualPaths.map((relative) => {
      const entry = entries[relative];
      if (
        !entry
        || typeof entry !== "object"
        || Array.isArray(entry)
        || typeof entry.sha256 !== "string"
        || !/^[0-9a-f]{64}$/.test(entry.sha256)
        || (requiredMode !== undefined && entry.mode !== requiredMode)
      ) {
        throw new Error(`blocked: installed managed hook ${label} provenance is invalid: ${relative}`);
      }
      return requiredMode === undefined
        ? [relative, entry.sha256]
        : [relative, entry.sha256, requiredMode];
    });
  };
  return {
    configs: normalize(contract.configs, MANAGED_HOOK_CONFIG_PATHS, "config"),
    scripts: normalize(
      contract.scripts,
      MANAGED_HOOK_SCRIPT_NAMES.map((name) => `.agent-flow/scripts/hooks/${name}`),
      "script",
      "executable",
    ),
  };
}

function managedHookContractCommitment(skillPlanHash, contract) {
  if (typeof skillPlanHash !== "string" || !/^[0-9a-f]{64}$/.test(skillPlanHash)) {
    throw new Error("blocked: managed hook commitment has an invalid skill plan hash");
  }
  const normalized = normalizedManagedHookContract(contract);
  return sha256Bytes(Buffer.from(JSON.stringify({
    version: MANAGED_HOOK_CONTRACT_COMMITMENT_VERSION,
    skill_plan_hash: skillPlanHash,
    configs: normalized.configs,
    scripts: normalized.scripts,
  }), "utf8"));
}

function assertManagedHookContractInstalled(root, payload) {
  if (
    payload?.managed_hook_contract_commitment_version !== MANAGED_HOOK_CONTRACT_COMMITMENT_VERSION
    || typeof payload?.managed_hook_contract_commitment !== "string"
    || !/^[0-9a-f]{64}$/.test(payload.managed_hook_contract_commitment)
  ) {
    throw new Error("blocked: installed managed hook commitment is invalid");
  }
  const computed = managedHookContractCommitment(
    payload.skill_plan_hash,
    payload.managed_hook_contract,
  );
  if (computed !== payload.managed_hook_contract_commitment) {
    throw new Error("blocked: installed managed hook commitment does not match provenance");
  }
  const normalized = normalizedManagedHookContract(payload.managed_hook_contract);
  const expectedScriptHashes = new Map(
    normalized.scripts.map(([relative, committedSha]) => [relative, committedSha]),
  );
  const expected = expectedManagedHookProjection();
  for (const [relative, committedSha] of normalized.configs) {
    const projection = managedHookProjection(
      root,
      readManagedHookSettingsStrict(root, relative),
      relative,
      expectedScriptHashes,
    );
    if (
      JSON.stringify(projection) !== JSON.stringify(expected)
      || sha256Bytes(managedHookProjectionBytes(projection)) !== committedSha
    ) {
      throw new Error(`blocked: installed managed hook settings changed: ${relative}`);
    }
  }
  const holdMs = Number.parseInt(
    process.env.AGENT_FLOW_TEST_HOLD_AFTER_MANAGED_HOOK_CONFIG_VALIDATION_MS ?? "0",
    10,
  );
  if (Number.isInteger(holdMs) && holdMs > 0 && holdMs <= 10_000) {
    fs.writeFileSync(
      path.join(root, ".agent-flow", "managed-hook-config-validation-ready"),
      "ready\n",
      "utf8",
    );
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, holdMs);
  }
  for (const [relative, committedSha] of normalized.scripts) {
    const file = path.join(root, ...relative.split("/"));
    requireInstalledRegularFile(root, file, `managed hook script ${relative}`);
    requireManagedHookScriptExecutable(file, relative);
    if (sha256Bytes(fs.readFileSync(file)) !== committedSha) {
      throw new Error(`blocked: installed managed hook script changed: ${relative}`);
    }
  }
}

function authenticatedManagedHostPayload(existingPayload) {
  if (!existingPayload) return undefined;
  return assertManagedHostFilesCommitment(existingPayload)
    ? existingPayload
    : undefined;
}

function isSupportedLegacyMigrationPayload(payload) {
  return Boolean(
    payload
    && typeof payload === "object"
    && !Array.isArray(payload)
    && payload.install_scope === "project"
    && payload.root === "."
    && typeof payload.installed_at === "string"
    && Array.isArray(payload.profiles)
    && Array.isArray(payload.selected_skills)
    && payload.skill_index?.path === ".agent-flow/skills/index.json"
    && payload.managed_hook_contract === undefined
    && payload.managed_host_files === undefined
    && payload.skill_plan_hash === undefined
  );
}

function inspectManagedHostDestination(root, relative) {
  const parts = validateManagedHostRelativePath(relative).split("/");
  let cursor = path.resolve(root);
  for (let index = 0; index < parts.length; index += 1) {
    cursor = path.join(cursor, parts[index]);
    const stat = lstatIfExists(cursor);
    if (!stat) {
      return { exists: false, sha256: null, mode: null };
    }
    if (stat.isSymbolicLink()) {
      throw new Error(`blocked: managed host file path may not use symlinks: ${relative}`);
    }
    const final = index === parts.length - 1;
    if ((final && !stat.isFile()) || (!final && !stat.isDirectory())) {
      throw new Error(`blocked: managed host file path has an invalid component: ${relative}`);
    }
  }
  const metadata = fs.statSync(cursor);
  if (metadata.nlink !== 1) {
    throw new Error(`blocked: managed host file may not be hard-linked: ${relative}`);
  }
  return {
    exists: true,
    sha256: sha256Bytes(fs.readFileSync(cursor)),
    mode: metadata.mode & 0o777,
  };
}

function preflightManagedHostFiles(root, existingPayload, { legacyMigration = false } = {}) {
  const previous = readManagedHostFileProvenance(existingPayload);
  const desired = desiredManagedHostFiles(root);
  const desiredByPath = new Map(desired.map((spec) => [spec.relative, spec]));
  const entries = [];
  for (const spec of desired) {
    const current = inspectManagedHostDestination(root, spec.relative);
    const prior = previous.get(spec.relative);
    if (current.exists && current.sha256 !== spec.sha256 && current.sha256 !== prior?.sha256) {
      if (!legacyMigration || LEGACY_MANAGED_HOST_HASHES[spec.relative] !== current.sha256) {
        throw new Error(`blocked: user-modified managed host file differs: ${spec.relative}`);
      }
    }
    entries.push({
      ...spec,
      action: current.exists && current.sha256 === spec.sha256 ? "keep" : "write",
      expectedCurrentHash: current.sha256,
      expectedCurrentMode: current.mode,
      desiredMode: current.exists ? current.mode : 0o644,
    });
  }
  for (const [relative, prior] of previous) {
    if (desiredByPath.has(relative)) {
      continue;
    }
    const current = inspectManagedHostDestination(root, relative);
    if (current.exists && current.sha256 !== prior.sha256) {
      throw new Error(`blocked: user-modified retired managed host file differs: ${relative}`);
    }
    entries.push({
      relative,
      destination: path.join(root, ...relative.split("/")),
      content: null,
      sha256: null,
      source: prior.source,
      action: current.exists ? "delete" : "keep",
      expectedCurrentHash: current.sha256,
      expectedCurrentMode: current.mode,
      desiredMode: null,
    });
  }
  const files = {};
  for (const spec of desired) {
    files[spec.relative] = { source: spec.source, sha256: spec.sha256 };
  }
  return {
    root: path.resolve(root),
    entries,
    manifest: { version: MANAGED_HOST_FILES_VERSION, files },
  };
}

function ensureManagedHostParent(root, relative) {
  const parts = validateManagedHostRelativePath(relative).split("/").slice(0, -1);
  let cursor = path.resolve(root);
  for (const part of parts) {
    cursor = path.join(cursor, part);
    const stat = lstatIfExists(cursor);
    if (!stat) {
      fs.mkdirSync(cursor);
      continue;
    }
    if (stat.isSymbolicLink() || !stat.isDirectory()) {
      throw new Error(`blocked: managed host file parent is unsafe: ${relative}`);
    }
  }
}

function writeManagedHostFile(plan, entry) {
  const current = inspectManagedHostDestination(plan.root, entry.relative);
  if (
    current.sha256 !== entry.expectedCurrentHash
    || current.mode !== entry.expectedCurrentMode
  ) {
    throw new Error(`blocked: managed host file changed during install: ${entry.relative}`);
  }
  if (entry.action === "keep") {
    return;
  }
  if (entry.action === "delete") {
    const prepared = preparePendingCriticalInstallMutation(entry.destination, null);
    assertPreparedCriticalInstallMutationUnchanged(prepared, entry.destination);
    fs.rmSync(entry.destination);
    checkpointPendingCriticalInstall(entry.destination);
    return;
  }
  ensureManagedHostParent(plan.root, entry.relative);
  const scratch = pendingProjectSkillHostTransaction?.persistent
    ? path.join(pendingProjectSkillHostTransaction.persistent.transactionRoot, "writes")
    : path.dirname(entry.destination);
  const temporary = path.join(
    scratch,
    `.${path.basename(entry.destination)}.agent-flow-${process.pid}-${crypto.randomBytes(6).toString("hex")}.tmp`,
  );
  fs.writeFileSync(temporary, entry.content, { flag: "wx", mode: 0o600 });
  fs.chmodSync(temporary, entry.desiredMode);
  try {
    const prepared = preparePendingCriticalInstallMutation(entry.destination, temporary);
    assertPreparedCriticalInstallMutationUnchanged(prepared, entry.destination);
    fs.renameSync(temporary, entry.destination);
    checkpointPendingCriticalInstall(entry.destination);
  } catch (error) {
    fs.rmSync(temporary, { force: true });
    throw error;
  }
}

function installHostReviewers(plan) {
  for (const entry of plan.entries) {
    if ([".Codex/agents/", ".claude/agents/", ".omp/agents/"]
      .some((prefix) => entry.relative.startsWith(prefix))) {
      writeManagedHostFile(plan, entry);
    }
  }
}

function assertManagedHostPlanApplied(plan) {
  for (const entry of plan.entries) {
    const current = inspectManagedHostDestination(plan.root, entry.relative);
    if (entry.sha256 === null) {
      if (current.exists) {
        throw new Error(`blocked: retired managed host file was not removed: ${entry.relative}`);
      }
    } else if (
      !current.exists
      || current.sha256 !== entry.sha256
      || current.mode !== entry.desiredMode
    ) {
      throw new Error(`blocked: managed host file install did not converge: ${entry.relative}`);
    }
  }
}

function copySkillSnapshot(src, dest, expectedHash) {
  if (fs.existsSync(path.join(dest, "SKILL.md"))) {
    const currentHash = hashSkillTree(dest);
    if (currentHash !== expectedHash) {
      throw new Error(`existing skill snapshot differs: ${path.basename(dest)}`);
    }
    return;
  }
  if (fs.existsSync(dest)) {
    throw new Error(`skill snapshot destination is not empty: ${dest}`);
  }
  const parent = path.dirname(dest);
  const installTransaction = pendingProjectSkillHostTransaction?.persistent;
  if (installTransaction) ensureInstallDirectoryWithProgress(installTransaction, parent);
  else fs.mkdirSync(parent, { recursive: true });
  const scratch = pendingProjectSkillHostTransaction?.persistent
    ? path.join(pendingProjectSkillHostTransaction.persistent.transactionRoot, "writes")
    : parent;
  const temp = fs.mkdtempSync(path.join(scratch, ".skill-snapshot-"));
  try {
    copyTreeBinarySafe(src, temp);
    const copiedHash = hashSkillTree(temp);
    if (copiedHash !== expectedHash) {
      throw new Error(`skill snapshot hash mismatch: ${path.basename(dest)}`);
    }
    const prepared = preparePendingCriticalInstallMutation(dest, temp);
    assertPreparedCriticalInstallMutationUnchanged(prepared, dest);
    fs.renameSync(temp, dest);
    if (process.env.AGENT_FLOW_TEST_CRASH_AFTER_SKILL_SNAPSHOT_RENAME === "1") process.exit(82);
    checkpointPendingCriticalInstall(dest);
  } finally {
    if (fs.existsSync(temp)) fs.rmSync(temp, { recursive: true, force: true });
  }
}

function replaceSkillSnapshot(src, dest, expectedHash) {
  const parent = path.dirname(dest);
  const installTransaction = pendingProjectSkillHostTransaction?.persistent;
  if (installTransaction) ensureInstallDirectoryWithProgress(installTransaction, parent);
  else fs.mkdirSync(parent, { recursive: true });
  const scratch = pendingProjectSkillHostTransaction?.persistent
    ? path.join(pendingProjectSkillHostTransaction.persistent.transactionRoot, "writes")
    : parent;
  const temp = fs.mkdtempSync(path.join(scratch, ".skill-upgrade-"));
  const backup = path.join(scratch, `.skill-upgrade-backup-${process.pid}-${Date.now()}`);
  let movedExisting = false;
  try {
    copyTreeBinarySafe(src, temp);
    if (hashSkillTree(temp) !== expectedHash) {
      throw new Error(`skill snapshot hash mismatch: ${path.basename(dest)}`);
    }
    if (fs.existsSync(dest)) {
      const prepared = preparePendingCriticalInstallMutation(dest, null);
      assertPreparedCriticalInstallMutationUnchanged(prepared, dest);
      fs.renameSync(dest, backup);
      movedExisting = true;
      checkpointPendingCriticalInstall(dest);
    }
    const prepared = preparePendingCriticalInstallMutation(dest, temp);
    assertPreparedCriticalInstallMutationUnchanged(prepared, dest);
    fs.renameSync(temp, dest);
    if (process.env.AGENT_FLOW_TEST_CRASH_AFTER_SKILL_SNAPSHOT_RENAME === "1") process.exit(82);
    checkpointPendingCriticalInstall(dest);
    if (movedExisting) fs.rmSync(backup, { recursive: true, force: true });
  } catch (error) {
    if (!fs.existsSync(dest) && movedExisting && fs.existsSync(backup)) {
      const restorePrepared = preparePendingCriticalInstallMutation(dest, backup);
      assertPreparedCriticalInstallMutationUnchanged(restorePrepared, dest);
      fs.renameSync(backup, dest);
      checkpointPendingCriticalInstall(dest);
    }
    throw error;
  } finally {
    if (fs.existsSync(temp)) fs.rmSync(temp, { recursive: true, force: true });
    if (fs.existsSync(backup) && fs.existsSync(dest)) {
      fs.rmSync(backup, { recursive: true, force: true });
    }
  }
}

function copyTreeBinarySafe(src, dest) {
  const sourceRoot = fs.lstatSync(src);
  if (sourceRoot.isSymbolicLink() || !sourceRoot.isDirectory()) {
    throw new Error(`source tree root must be a regular directory: ${src}`);
  }
  const destinationRoot = lstatIfExists(dest);
  if (destinationRoot && (destinationRoot.isSymbolicLink() || !destinationRoot.isDirectory())) {
    throw new Error(`source tree destination must be a regular directory: ${dest}`);
  }
  if (!destinationRoot) fs.mkdirSync(dest, { mode: 0o700 });
  for (const name of fs.readdirSync(src).sort()) {
    const source = path.join(src, name);
    const target = path.join(dest, name);
    const metadata = fs.lstatSync(source);
    if (metadata.isSymbolicLink()) {
      throw new Error(`source tree may not contain symlinks: ${source}`);
    }
    if (metadata.isDirectory()) {
      copyTreeBinarySafe(source, target);
    } else if (metadata.isFile()) {
      fs.copyFileSync(source, target, fs.constants.COPYFILE_EXCL);
      fs.chmodSync(target, metadata.mode & 0o777);
    } else {
      throw new Error(`source tree may not contain special files: ${source}`);
    }
  }
  fs.chmodSync(dest, sourceRoot.mode & 0o777);
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
  skippedRootDirs = new Set(),
) {
  if (!fs.existsSync(src)) {
    return;
  }
  const installTransaction = pendingProjectSkillHostTransaction?.persistent;
  if (installTransaction) ensureInstallDirectoryWithProgress(installTransaction, dest);
  else fs.mkdirSync(dest, { recursive: true });
  const sourceNames = new Set();
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const srcPath = path.join(src, entry.name);
    const destPath = path.join(dest, entry.name);
    if (entry.name === "__pycache__" || entry.name.endsWith(".pyc")) {
      if (force && lstatIfExists(destPath)) {
        const prepared = preparePendingCriticalInstallMutation(destPath, null);
        assertPreparedCriticalInstallMutationUnchanged(prepared, destPath);
        fs.rmSync(destPath, { recursive: true, force: true });
        checkpointPendingCriticalInstall(destPath);
      }
      continue;
    }
    sourceNames.add(entry.name);
    if (entry.isDirectory()) {
      if (isRoot && skippedRootDirs.has(entry.name)) {
        continue;
      }
      if (isRoot && allowedRootDirs && !allowedRootDirs.has(entry.name)) {
        removeManagedDirIfSame(srcPath, destPath, force);
        continue;
      }
      if (isRoot && excludedRootDirs.has(entry.name)) {
        removeManagedDirIfSame(srcPath, destPath, force);
        continue;
      }
      copyBundledDirIfMissingOrSame(srcPath, destPath, force, excludedRootDirs, false, pruneExtraneous, preservedExtraneousRootNames, null, new Set());
      continue;
    }
    if (!entry.isFile()) {
      continue;
    }
    const content = fs.readFileSync(srcPath);
    writeManagedFileIfMissingOrSame(destPath, content, force);
  }
  if (force && pruneExtraneous) {
    for (const entry of fs.readdirSync(dest, { withFileTypes: true })) {
      if (!sourceNames.has(entry.name) && !(isRoot && preservedExtraneousRootNames.has(entry.name))) {
        const removed = path.join(dest, entry.name);
        const prepared = preparePendingCriticalInstallMutation(removed, null);
        assertPreparedCriticalInstallMutationUnchanged(prepared, removed);
        fs.rmSync(removed, { recursive: true, force: true });
        checkpointPendingCriticalInstall(removed);
      }
    }
  }
}

function removeManagedDirIfSame(src, dest, force = false) {
  if (!fs.existsSync(dest)) {
    return;
  }
  if (!force && !dirContentsMatch(src, dest)) {
    return;
  }
  const prepared = preparePendingCriticalInstallMutation(dest, null);
  assertPreparedCriticalInstallMutationUnchanged(prepared, dest);
  fs.rmSync(dest, { recursive: true, force: true });
  checkpointPendingCriticalInstall(dest);
}

function removeStaleContextDocsScripts(agentFlowDir, force = false) {
  if (!force) {
    return;
  }
  for (const filename of ["check-context-docs.mjs", "check-context-docs.ts"]) {
    const removed = path.join(agentFlowDir, "scripts", filename);
    const prepared = preparePendingCriticalInstallMutation(removed, null);
    assertPreparedCriticalInstallMutationUnchanged(prepared, removed);
    fs.rmSync(removed, { force: true });
    checkpointPendingCriticalInstall(removed);
  }
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
    if (srcEntry.isFile() && !fs.readFileSync(srcPath).equals(fs.readFileSync(destPath))) {
      return false;
    }
  }
  return true;
}

function installProjectSkills(root, agentFlowDir, previousIndex, force = false, installSelection = null, skillPlan = null) {
  const selected = selectProjectSkills(root, agentFlowDir, installSelection, skillPlan);
  const externalExposureSkillNames = new Set(
    normalizedExternalExposureSkillNames(selected.selection),
  );
  const intendedLinks = [];
  for (const skill of selected.skills) {
    const exposedExternal = externalExposureSkillNames.has(portableSkillCasefold(skill.name))
      && ["host-bootstrap", "shared"].includes(skill.source);
    // bundled skill 중 host 디렉토리 link 대상은 BUNDLED_HOST_SKILL_NAMES뿐이다.
    // 나머지 bundled skill은 index에만 노출해 agent가 발견할 수 있게 한다.
    if (
      !["local", "project"].includes(skill.discovery_source ?? skill.source)
      && !BUNDLED_HOST_SKILL_NAMES.has(skill.name)
      && !exposedExternal
    ) {
      continue;
    }
    const projectLocal = ["local", "project"].includes(skill.discovery_source ?? skill.source);
    const targetHosts = exposedExternal || projectLocal ? PROJECT_SKILL_HOSTS : skill.hosts;
    for (const host of targetHosts) {
      intendedLinks.push({ skill, host });
    }
  }
  const hostPlan = preflightProjectSkillHostLinks(
    root,
    intendedLinks,
    previousIndex,
  );
  if (!pendingProjectSkillHostTransaction) {
    throw new Error("blocked: project install transaction is missing");
  }
  const persistent = pendingProjectSkillHostTransaction.persistent;
  registerProjectSkillHostTransaction(persistent, hostPlan);
  let hostTransaction;
  try {
    hostTransaction = applyProjectSkillHostLinkPlan(hostPlan, persistent);
  } catch (error) {
    throw error;
  }
  pendingProjectSkillHostTransaction.hostTransaction = hostTransaction;
  try {
    const index = { ...selected, links: hostTransaction.results };
    writeManagedFile(
      path.join(agentFlowDir, "skills", "index.json"),
      `${JSON.stringify(index, null, 2)}\n`,
    );
    return index;
  } catch (error) {
    rollbackPendingProjectSkillHostTransaction();
    throw error;
  }
}

function managedLegacyRootScriptCriticalPaths(root) {
  if (samePath(root, KIT_ROOT)) return [];
  const destination = path.join(root, "scripts");
  const metadata = lstatIfExists(destination);
  if (!metadata || metadata.isSymbolicLink() || !metadata.isDirectory()) return [];
  return dirContentsMatch(path.join(KIT_ROOT, "scripts"), destination) ? ["scripts"] : [];
}

function criticalInstallPaths(root, agentFlowDir, dynamicManagedHostPaths = []) {
  const specs = [
    ...[
      "workflows",
      "skills",
      "profiles",
      "templates",
      "scripts",
      "runtime",
      "prompts",
      "rules",
      "bootstrap",
      "bin",
    ].map((name) => ({ path: path.join(agentFlowDir, name), kind: "directory" })),
    { path: path.join(agentFlowDir, "kit.json"), kind: "file" },
    ...REQUIRED_MANAGED_HOST_FILES.map((relative) => ({
      path: path.join(root, ...relative.split("/")),
      kind: "file",
    })),
    ...MANAGED_HOOK_CONFIG_PATHS.map((relative) => ({
      path: path.join(root, ...relative.split("/")),
      kind: "file",
    })),
    { path: path.join(root, ".Codex", "rules", "context"), kind: "directory" },
    { path: path.join(root, ".Codex", "context"), kind: "directory" },
    { path: path.join(root, ".Codex", "rules", "codebase-rubric.md"), kind: "file" },
    { path: path.join(root, ".Codex", "rules", "concise-output.md"), kind: "file" },
    { path: path.join(root, ".gitignore"), kind: "file" },
    { path: path.join(root, "AGENTS.md"), kind: "file" },
    { path: path.join(root, "CLAUDE.md"), kind: "file" },
  ];
  const known = new Set(specs.map((spec) => installTransactionRelative(root, spec.path)));
  for (const relative of dynamicManagedHostPaths) {
    const legacyRootDirectory = relative === "scripts";
    const validated = legacyRootDirectory ? relative : validateManagedHostRelativePath(relative);
    if (!legacyRootDirectory && !isDynamicManagedHostCriticalPath(validated)) {
      throw new Error(`blocked: dynamic managed host path is outside the managed namespace: ${validated}`);
    }
    if (known.has(validated)) continue;
    known.add(validated);
    specs.push({
      path: path.join(root, ...validated.split("/")),
      kind: legacyRootDirectory ? "directory" : "file",
    });
  }
  return specs;
}

function isDynamicManagedHostCriticalPath(relative) {
  return [
    ".Codex/agents/",
    ".claude/agents/",
    ".omp/agents/",
    ".omp/extensions/",
  ].some((prefix) => relative.startsWith(prefix));
}

function installTransactionRoot(agentFlowDir) {
  return path.join(agentFlowDir, "install-transaction");
}

function assertNoOpenInstallTransaction(root) {
  const transactionRoot = installTransactionRoot(path.join(root, ".agent-flow"));
  const metadata = lstatIfExists(transactionRoot);
  if (!metadata) return;
  if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
    throw new Error(`blocked: install transaction root is unsafe: ${transactionRoot}`);
  }
  throw new Error("blocked: project install transaction is in progress; retry after install recovery completes");
}

function recoverStaleNodeInstallStartLock(root) {
  const lockPath = projectStartLockPath(root);
  const metadata = lstatIfExists(lockPath);
  if (!metadata) return;
  if (metadata.isSymbolicLink() || !metadata.isFile()) return;
  let payload;
  try {
    payload = JSON.parse(fs.readFileSync(lockPath, "utf8"));
  } catch {
    return;
  }
  if (
    !payload
    || typeof payload !== "object"
    || Array.isArray(payload)
    || !hasExactObjectKeys(payload, START_LOCK_KEYS)
    || payload.version !== START_LOCK_VERSION
    || payload.runtime !== "node-install"
    || !Number.isSafeInteger(payload.pid)
    || payload.pid <= 0
    || typeof payload.token !== "string"
    || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(payload.token)
    || typeof payload.created_at !== "string"
    || Number.isNaN(Date.parse(payload.created_at))
    || typeof payload.project_root !== "string"
    || !samePath(payload.project_root, root)
    || processIsAlive(payload.pid)
  ) {
    return;
  }
  const currentMetadata = lstatIfExists(lockPath);
  if (
    !currentMetadata
    || currentMetadata.isSymbolicLink()
    || !currentMetadata.isFile()
    || currentMetadata.dev !== metadata.dev
    || currentMetadata.ino !== metadata.ino
  ) {
    return;
  }
  let current;
  try {
    current = JSON.parse(fs.readFileSync(lockPath, "utf8"));
  } catch {
    return;
  }
  if (current?.token === payload.token && current?.pid === payload.pid && current?.runtime === "node-install") {
    fs.unlinkSync(lockPath);
  }
}

function installTransactionRelative(root, candidate) {
  return path.relative(root, candidate).split(path.sep).join("/");
}

function beginCriticalInstallTransaction(root, agentFlowDir, dynamicManagedHostPaths = []) {
  const transactionRoot = installTransactionRoot(agentFlowDir);
  const agentFlowMetadata = lstatIfExists(agentFlowDir);
  if (agentFlowMetadata && (agentFlowMetadata.isSymbolicLink() || !agentFlowMetadata.isDirectory())) {
    throw new Error(`blocked: install transaction parent is unsafe: ${agentFlowDir}`);
  }
  if (!agentFlowMetadata) {
    fs.mkdirSync(agentFlowDir, { mode: 0o700 });
    fsyncDirectoryPath(root);
  }
  let ownedIdentity = null;
  const ownerToken = crypto.randomBytes(16).toString("hex");
  try {
    try {
      fs.mkdirSync(transactionRoot, { mode: 0o700 });
    } catch (error) {
      if (error?.code === "EEXIST") {
        throw new Error(`blocked: unresolved install transaction remains: ${transactionRoot}`);
      }
      throw error;
    }
    fsyncDirectoryPath(agentFlowDir);
    const transactionMetadata = fs.lstatSync(transactionRoot);
    ownedIdentity = { dev: transactionMetadata.dev, ino: transactionMetadata.ino };
    writeJson(path.join(transactionRoot, "owner.json"), {
      version: INSTALL_TRANSACTION_OWNER_VERSION,
      pid: process.pid,
      token: ownerToken,
      created_at: new Date().toISOString(),
    });
    const holdMs = Number.parseInt(process.env.AGENT_FLOW_TEST_HOLD_INSTALL_TRANSACTION_LOCK_MS ?? "0", 10);
    if (Number.isInteger(holdMs) && holdMs > 0 && holdMs <= 10_000) {
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, holdMs);
    }
    fs.mkdirSync(path.join(transactionRoot, "files"), { mode: 0o700 });
    fs.mkdirSync(path.join(transactionRoot, "hosts"), { mode: 0o700 });
    fs.mkdirSync(path.join(transactionRoot, "writes"), { mode: 0o700 });
    const snapshots = criticalInstallPaths(root, agentFlowDir, dynamicManagedHostPaths).map((spec, index) => {
      assertInstallTransactionParentSafe(root, spec.path, "critical install path");
      const metadata = lstatIfExists(spec.path);
      const relative = installTransactionRelative(root, spec.path);
      if (!relative || relative.startsWith("../") || path.isAbsolute(relative)) {
        throw new Error(`blocked: critical install path escapes the project: ${spec.path}`);
      }
      if (!metadata) {
        return {
          file: spec.path,
          relative,
          kind: spec.kind,
          existed: false,
          backup: null,
          mode: null,
          backupSha256: null,
          backupTreeHash: null,
          originalState: { kind: "absent" },
        };
      }
      const kindMatches = spec.kind === "file" ? metadata.isFile() : metadata.isDirectory();
      if (metadata.isSymbolicLink() || !kindMatches) {
        throw new Error(`blocked: critical install ${spec.kind} is unsafe: ${spec.path}`);
      }
      const backup = path.join(
        transactionRoot,
        "files",
        spec.kind === "file" ? `${index}.bin` : `${index}.tree`,
      );
      if (spec.kind === "file") {
        fs.copyFileSync(spec.path, backup, fs.constants.COPYFILE_EXCL);
        fs.chmodSync(backup, metadata.mode & 0o777);
      } else {
        copyTreeBinarySafe(spec.path, backup);
      }
      const originalState = installTransactionPathState(spec.path, spec.kind, "critical install path");
      const backupState = installTransactionPathState(backup, spec.kind, "critical install backup");
      if (!installTransactionStatesEqual(originalState, backupState)) {
        throw new Error(`blocked: critical install backup did not preserve bytes and modes: ${spec.path}`);
      }
      return {
        file: spec.path,
        relative,
        kind: spec.kind,
        existed: true,
        backup,
        mode: metadata.mode & 0o777,
        backupSha256: spec.kind === "file" ? backupState.sha256 : null,
        backupTreeHash: spec.kind === "directory" ? backupState.tree_hash : null,
        originalState,
      };
    });
    const journal = {
      version: INSTALL_TRANSACTION_VERSION,
      status: "open",
      commit_proof: null,
      codex_trust: null,
      files: snapshots.map((snapshot) => ({
        path: snapshot.relative,
        kind: snapshot.kind,
        existed: snapshot.existed,
        backup: snapshot.backup ? installTransactionRelative(root, snapshot.backup) : null,
        mode: snapshot.mode,
        backup_sha256: snapshot.backupSha256,
        backup_tree_hash: snapshot.backupTreeHash,
        applied_state: snapshot.originalState,
        pending_state: null,
        recovery_state: null,
      })),
      hosts: [],
    };
    const journalPath = path.join(transactionRoot, "journal.json");
    writeInstallTransactionJournal(journalPath, journal);
    return {
      root,
      transactionRoot,
      journalPath,
      journal,
      snapshots,
      hostBackups: new Map(),
      ownerToken,
      ownedIdentity,
    };
  } catch (error) {
    removeOwnedInstallTransaction(transactionRoot, ownedIdentity, ownerToken);
    throw error;
  }
}

function recordCriticalInstallAppliedStates(persistent) {
  if (persistent.journal.status !== "open") {
    throw new Error("blocked: cannot update a closed install transaction");
  }
  if (persistent.journal.files.length !== persistent.snapshots.length) {
    throw new Error("blocked: critical install transaction snapshot mismatch");
  }
  for (let index = 0; index < persistent.snapshots.length; index += 1) {
    const snapshot = persistent.snapshots[index];
    const journalEntry = persistent.journal.files[index];
    if (journalEntry.pending_state !== null) {
      throw new Error(`blocked: critical install mutation is unresolved: ${snapshot.relative}`);
    }
    const currentState = installTransactionPathState(
      snapshot.file,
      snapshot.kind,
      "critical install assertion path",
    );
    const appliedState = validateInstallTransactionState(journalEntry.applied_state, snapshot.kind, true);
    if (!installTransactionStatesEqual(currentState, appliedState)) {
      throw new Error(`blocked: critical install destination changed during install: ${snapshot.relative}`);
    }
  }
}

function ensureInstallDirectoryWithProgress(persistent, directory) {
  const metadata = lstatIfExists(directory);
  if (metadata) {
    if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
      throw new Error(`blocked: install directory is unsafe: ${directory}`);
    }
    return;
  }
  const containing = persistent.snapshots.find((snapshot) => (
    snapshot.kind === "directory"
    && (
      path.resolve(snapshot.file) === path.resolve(directory)
      || installTransactionPathContains(snapshot.file, directory)
    )
  ));
  if (!containing) {
    fs.mkdirSync(directory, { recursive: true });
    return;
  }
  if (!lstatIfExists(path.dirname(containing.file))) {
    assertInstallTransactionParentSafe(persistent.root, containing.file, "critical install directory");
    fs.mkdirSync(path.dirname(containing.file), { recursive: true });
  }
  const targets = [];
  let cursor = path.resolve(containing.file);
  if (!lstatIfExists(cursor)) targets.push(cursor);
  const relative = path.relative(cursor, path.resolve(directory));
  for (const part of relative.split(path.sep).filter(Boolean)) {
    cursor = path.join(cursor, part);
    if (!lstatIfExists(cursor)) targets.push(cursor);
  }
  const scratch = path.join(persistent.transactionRoot, "writes");
  for (const target of targets) {
    const temporary = fs.mkdtempSync(path.join(scratch, ".directory-"));
    fs.chmodSync(temporary, 0o777 & ~process.umask());
    const prepared = preparePendingCriticalInstallMutation(target, temporary);
    assertPreparedCriticalInstallMutationUnchanged(prepared, target);
    fs.renameSync(temporary, target);
    checkpointPendingCriticalInstall(target);
  }
}

function installTransactionPathContains(parent, child) {
  const relative = path.relative(path.resolve(parent), path.resolve(child));
  return relative !== "" && !relative.startsWith("..") && !path.isAbsolute(relative);
}

function registerProjectSkillHostTransaction(persistent, hostPlan) {
  if (persistent.journal.hosts.length > 0 || persistent.hostBackups.size > 0) {
    throw new Error("blocked: project skill host transaction is already registered");
  }
  persistent.hostPlan = hostPlan;
  let hostIndex = 0;
  for (const action of hostPlan.actions) {
    if (action.action === "keep") continue;
    const destination = installTransactionRelative(persistent.root, action.destination);
    if (!destination || destination.startsWith("../") || path.isAbsolute(destination)) {
      throw new Error(`blocked: project skill host destination escapes the project: ${action.destination}`);
    }
    const backup = ["replace", "delete"].includes(action.action)
      ? path.join(persistent.transactionRoot, "hosts", `${hostIndex}.backup`)
      : null;
    hostIndex += 1;
    persistent.hostBackups.set(action.destination, backup);
    persistent.journal.hosts.push({
      name: action.name,
      host: action.host,
      destination,
      action: action.action,
      expected_kind: action.expected.kind,
      expected_target: action.expected.kind === "symlink" ? action.expected.target : null,
      expected_tree_hash: action.expected.kind === "directory" ? action.expected.treeHash : null,
      expected_tree_integrity: action.expected.kind === "directory" ? action.expected.integrityHash : null,
      backup: backup ? installTransactionRelative(persistent.root, backup) : null,
      source: action.sourceDir ? installTransactionRelative(persistent.root, action.sourceDir) : null,
      source_tree_hash: action.sourceTreeHash ?? null,
      source_tree_integrity: action.sourceIntegrityHash ?? null,
      progress: "pending",
      recovery_state: null,
    });
  }
  writeInstallTransactionJournal(persistent.journalPath, persistent.journal);
}

function updateInstallTransactionHostProgress(persistent, destination, progress) {
  const relative = installTransactionRelative(persistent.root, destination);
  const entry = persistent.journal.hosts.find((candidate) => candidate.destination === relative);
  if (!entry || !["pending", "backup-intent", "backed-up", "apply-intent", "applied"].includes(progress)) {
    throw new Error(`blocked: invalid project skill host transaction progress: ${relative}`);
  }
  entry.progress = progress;
  writeInstallTransactionJournal(persistent.journalPath, persistent.journal);
}

function restoreCriticalInstallFileSnapshots(snapshots) {
  const errors = [];
  for (const snapshot of [...snapshots].reverse()) {
    try {
      const current = lstatIfExists(snapshot.file);
      if (current) fs.rmSync(snapshot.file, { recursive: true, force: true });
      if (snapshot.existed) {
        fs.mkdirSync(path.dirname(snapshot.file), { recursive: true });
        if (snapshot.kind === "file") {
          fs.copyFileSync(snapshot.backup, snapshot.file, fs.constants.COPYFILE_EXCL);
        } else {
          fs.renameSync(snapshot.backup, snapshot.file);
        }
        fs.chmodSync(snapshot.file, snapshot.mode);
      }
    } catch (error) {
      errors.push(`${snapshot.file}: ${error instanceof Error ? error.message : String(error)}`);
    }
  }
  return errors;
}

function registerCodexTrustObligation(persistent, plan) {
  if (persistent.journal.status !== "open" || persistent.journal.codex_trust !== null) {
    throw new Error("blocked: Codex trust obligation is already registered");
  }
  persistent.journal.codex_trust = {
    version: plan.version,
    config_path: plan.configPath,
    managed_hook_contract_commitment: plan.managedHookContractCommitment,
    skill_plan_hash: plan.skillPlanHash,
    updates: plan.updates,
  };
  writeInstallTransactionJournal(persistent.journalPath, persistent.journal);
}

function commitPendingProjectSkillHostTransaction({ afterCommit = null } = {}) {
  if (!pendingProjectSkillHostTransaction) return;
  const transaction = pendingProjectSkillHostTransaction;
  if (!transaction.hostTransaction) {
    throw new Error("blocked: project skill host transaction was not applied");
  }
  const commitLock = acquireProjectStartLock(transaction.root, "node-install");
  try {
    recordCriticalInstallAppliedStates(transaction.persistent);
    assertCriticalInstallHostExposuresApplied(transaction.persistent);
    if (process.env.AGENT_FLOW_TEST_FAIL_BEFORE_INSTALL_COMMIT === "1") {
      throw new Error("injected install commit failure");
    }
    transaction.persistent.journal.commit_proof = installTransactionCommitProof(
      transaction.root,
      transaction.persistent.journal,
    );
    transaction.persistent.journal.status = "committed";
    writeInstallTransactionJournal(
      transaction.persistent.journalPath,
      transaction.persistent.journal,
    );
    if (typeof afterCommit === "function") afterCommit();
    pendingProjectSkillHostTransaction = null;
    const holdMs = Number.parseInt(process.env.AGENT_FLOW_TEST_HOLD_AFTER_INSTALL_COMMIT_MS ?? "0", 10);
    if (Number.isInteger(holdMs) && holdMs > 0 && holdMs <= 10_000) {
      fs.writeFileSync(
        path.join(transaction.persistent.transactionRoot, "commit-cleanup-ready"),
        "ready\n",
        "utf8",
      );
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, holdMs);
    }
    if (process.env.AGENT_FLOW_TEST_FAIL_AFTER_INSTALL_COMMIT === "1") {
      throw new Error("injected install cleanup failure after commit");
    }
    const cleanupRoot = detachValidatedInstallTransaction(
      transaction.persistent.transactionRoot,
      transaction.persistent.ownedIdentity,
      { token: transaction.persistent.ownerToken, pid: process.pid },
    );
    if (!cleanupRoot) {
      throw new Error("blocked: committed install transaction identity changed before cleanup");
    }
    fs.rmSync(cleanupRoot, { recursive: true, force: true });
  } finally {
    releaseProjectStartLock(commitLock);
  }
}

function assertCriticalInstallHostExposuresApplied(persistent) {
  if (!persistent.hostPlan || !Array.isArray(persistent.hostPlan.actions)) {
    throw new Error("blocked: project skill host transaction plan is missing");
  }
  for (const action of persistent.hostPlan.actions) {
    const current = projectSkillHostDestinationSnapshot(action.destination);
    if (action.action === "keep") {
      if (!projectSkillHostSnapshotsEqual(current, action.expected)) {
        throw new Error(`blocked: project skill host destination changed before commit: ${action.path}`);
      }
    } else if (action.action === "delete") {
      if (current.kind !== "absent") {
        throw new Error(`blocked: project skill host destination changed before commit: ${action.path}`);
      }
    } else if (!projectSkillHostActionMatchesApplied(action, current)) {
      throw new Error(`blocked: project skill host destination changed before commit: ${action.path}`);
    }
    if (action.sourceDir && hashSkillTree(action.sourceDir) !== action.sourceTreeHash) {
      throw new Error(`blocked: project skill source changed before commit: ${action.name}`);
    }
    if (
      action.sourceDir
      && installTransactionTreeIntegrity(action.sourceDir) !== action.sourceIntegrityHash
    ) {
      throw new Error(`blocked: project skill source modes changed before commit: ${action.name}`);
    }
  }
}

function projectSkillHostActionMatchesApplied(action, current) {
  if (!action.sourceDir || !action.sourceTreeHash) return false;
  return current.kind === "symlink"
    ? current.target === path.relative(path.dirname(action.destination), action.sourceDir)
    : current.kind === "directory"
      && current.treeHash === action.sourceTreeHash
      && current.integrityHash === action.sourceIntegrityHash;
}

function rollbackPendingProjectSkillHostTransaction() {
  if (!pendingProjectSkillHostTransaction) return [];
  const transaction = pendingProjectSkillHostTransaction;
  pendingProjectSkillHostTransaction = null;
  if (transaction.persistent.journal.status === "committed") {
    return [];
  }
  const errors = [];
  try {
    recoverInterruptedInstallTransaction(
      transaction.persistent.root,
      path.join(transaction.persistent.root, ".agent-flow"),
      { ownerToken: transaction.persistent.ownerToken },
    );
  } catch (error) {
    errors.push(error instanceof Error ? error.message : String(error));
  }
  return errors;
}

function recoverInterruptedInstallTransaction(root, agentFlowDir, { ownerToken = null } = {}) {
  const transactionRoot = installTransactionRoot(agentFlowDir);
  const metadata = lstatIfExists(transactionRoot);
  if (!metadata) return;
  if (
    metadata.isSymbolicLink()
    || !metadata.isDirectory()
    || pathHasSymlink(root, transactionRoot)
  ) {
    throw new Error(`blocked: install transaction root is unsafe: ${transactionRoot}`);
  }
  const journalPath = path.join(transactionRoot, "journal.json");
  if (!lstatIfExists(journalPath)) {
    recoverUnpublishedInstallTransaction(root, transactionRoot, metadata);
    return;
  }
  const journal = readStrictInstalledObject(root, journalPath, "install transaction journal");
  if (
    !hasExactObjectKeys(journal, ["codex_trust", "commit_proof", "files", "hosts", "status", "version"])
    || journal.version !== INSTALL_TRANSACTION_VERSION
    || !["open", "committed"].includes(journal.status)
    || !Array.isArray(journal.hosts)
    || !Array.isArray(journal.files)
  ) {
    throw new Error("blocked: install transaction journal is invalid");
  }
  if (journal.status === "open" && journal.commit_proof !== null) {
    throw new Error("blocked: open install transaction has an invalid commit proof");
  }
  if (journal.status === "committed") {
    if (
      typeof journal.commit_proof !== "string"
      || !/^[0-9a-f]{64}$/.test(journal.commit_proof)
      || installTransactionCommitProof(root, journal) !== journal.commit_proof
    ) {
      throw new Error("blocked: committed install transaction proof is invalid");
    }
  }
  const ownerMetadata = lstatIfExists(path.join(transactionRoot, "owner.json"));
  const owner = ownerMetadata
    ? readValidatedInstallTransactionOwner(root, transactionRoot)
    : null;
  if (!owner) {
    throw new Error("blocked: install transaction owner is missing");
  }
  const authorizedInProcessRollback = ownerToken !== null
    && owner?.token === ownerToken
    && owner?.pid === process.pid;
  if (ownerToken !== null && !authorizedInProcessRollback) {
    throw new Error("blocked: install transaction owner does not match the rollback caller");
  }
  if (owner && processIsAlive(owner.pid) && !authorizedInProcessRollback) {
    throw new Error(`blocked: install transaction is active in process ${owner.pid}`);
  }
  if (journal.status === "committed") {
    preflightInterruptedInstallRecovery(root, agentFlowDir, journal, {
      committed: true,
      ownerToken: owner?.token ?? null,
    });
    assertCommittedInstallFinalState(root);
    validatedCodexTrustObligation(root, journal.codex_trust);
    const cleanupRoot = detachValidatedInstallTransaction(
      transactionRoot,
      { dev: metadata.dev, ino: metadata.ino },
      owner,
    );
    if (!cleanupRoot) {
      throw new Error("blocked: committed install transaction identity changed before cleanup");
    }
    fs.rmSync(cleanupRoot, { recursive: true, force: true });
    return;
  }
  const recovery = preflightInterruptedInstallRecovery(root, agentFlowDir, journal, {
    ownerToken: owner.token,
  });
  const recoveryHoldMs = Number.parseInt(
    process.env.AGENT_FLOW_TEST_HOLD_AFTER_RECOVERY_PREFLIGHT_MS ?? "0",
    10,
  );
  if (Number.isInteger(recoveryHoldMs) && recoveryHoldMs > 0 && recoveryHoldMs <= 10_000) {
    fs.writeFileSync(path.join(transactionRoot, "recovery-preflight-ready"), "ready\n", "utf8");
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, recoveryHoldMs);
  }
  const recoveryScratch = path.join(transactionRoot, "writes");
  assertExistingInstallTransactionDirectoryChain(root, transactionRoot, recoveryScratch, "recovery scratch");
  let recoveryIndex = 0;
  for (const operation of [...recovery.hosts].reverse()) {
    if (operation.restoreBackup) {
      const staged = stageInstallTransactionRecoveryPath(
        operation.backup,
        recoveryScratch,
        `host-${recoveryIndex}`,
        "host",
        operation.originalState,
      );
      fs.mkdirSync(path.dirname(operation.destination), { recursive: true });
      if (prepareInstallTransactionRecoveryMutation(
        root,
        journalPath,
        journal,
        "hosts",
        operation.journalIndex,
        () => assertInstallTransactionHostRecoveryObserved(operation),
      )) {
        assertInstallTransactionRecoveryBackupObserved(operation, "host");
        moveInstallTransactionRecoveryDestinationAside(
          operation.destination,
          recoveryScratch,
          `host-${recoveryIndex}`,
        );
        fs.renameSync(staged, operation.destination);
      }
    } else if (operation.removeApplied && lstatIfExists(operation.destination)) {
      if (prepareInstallTransactionRecoveryMutation(
        root,
        journalPath,
        journal,
        "hosts",
        operation.journalIndex,
        () => assertInstallTransactionHostRecoveryObserved(operation),
      )) {
        moveInstallTransactionRecoveryDestinationAside(
          operation.destination,
          recoveryScratch,
          `host-${recoveryIndex}`,
        );
      }
    }
    recoveryIndex += 1;
  }
  for (const operation of [...recovery.files].reverse()) {
    if (!operation.restore) continue;
    if (operation.existed) {
      const staged = stageInstallTransactionRecoveryPath(
        operation.backup,
        recoveryScratch,
        `file-${recoveryIndex}`,
        operation.kind,
        operation.originalState,
      );
      fs.mkdirSync(path.dirname(operation.destination), { recursive: true });
      if (prepareInstallTransactionRecoveryMutation(
        root,
        journalPath,
        journal,
        "files",
        operation.journalIndex,
        () => assertInstallTransactionFileRecoveryObserved(operation),
      )) {
        assertInstallTransactionRecoveryBackupObserved(operation, operation.kind);
        moveInstallTransactionRecoveryDestinationAside(
          operation.destination,
          recoveryScratch,
          `file-${recoveryIndex}`,
        );
        fs.renameSync(staged, operation.destination);
        fs.chmodSync(operation.destination, operation.mode);
      }
    } else {
      if (prepareInstallTransactionRecoveryMutation(
        root,
        journalPath,
        journal,
        "files",
        operation.journalIndex,
        () => assertInstallTransactionFileRecoveryObserved(operation),
      )) {
        moveInstallTransactionRecoveryDestinationAside(
          operation.destination,
          recoveryScratch,
          `file-${recoveryIndex}`,
        );
      }
    }
    recoveryIndex += 1;
  }
  const cleanupRoot = detachValidatedInstallTransaction(
    transactionRoot,
    { dev: metadata.dev, ino: metadata.ino },
    owner,
  );
  if (!cleanupRoot) {
    throw new Error("blocked: recovered install transaction identity changed before cleanup");
  }
  if (process.env.AGENT_FLOW_TEST_CRASH_AFTER_RECOVERY_TRANSACTION_DETACH === "1") {
    process.exit(79);
  }
  fs.rmSync(cleanupRoot, { recursive: true, force: true });
}

function prepareInstallTransactionRecoveryMutation(root, journalPath, journal, collection, index, assertObserved) {
  if (!assertObserved()) return false;
  markInstallTransactionRecoveryIntent(root, journalPath, journal, collection, index);
  try {
    if (assertObserved()) return true;
    clearInstallTransactionRecoveryIntent(root, journalPath, journal, collection, index);
    return false;
  } catch (error) {
    clearInstallTransactionRecoveryIntent(root, journalPath, journal, collection, index);
    throw error;
  }
}

function markInstallTransactionRecoveryIntent(root, journalPath, journal, collection, index) {
  const indexes = installTransactionRecoveryAliasIndexes(root, journal, collection, index);
  let changed = false;
  for (const aliasIndex of indexes) {
    const entry = journal[collection]?.[aliasIndex];
    if (!entry || ![null, "restore-intent"].includes(entry.recovery_state)) {
      throw new Error("blocked: install transaction recovery journal is invalid");
    }
    if (entry.recovery_state === null) {
      entry.recovery_state = "restore-intent";
      changed = true;
    }
  }
  if (!changed) return;
  writeInstallTransactionJournal(journalPath, journal);
}

function clearInstallTransactionRecoveryIntent(root, journalPath, journal, collection, index) {
  const indexes = installTransactionRecoveryAliasIndexes(root, journal, collection, index);
  let changed = false;
  for (const aliasIndex of indexes) {
    const entry = journal[collection]?.[aliasIndex];
    if (entry?.recovery_state === "restore-intent") {
      entry.recovery_state = null;
      changed = true;
    }
  }
  if (!changed) return;
  writeInstallTransactionJournal(journalPath, journal);
}

function installTransactionRecoveryAliasIndexes(root, journal, collection, index) {
  const entries = journal[collection];
  const entry = entries?.[index];
  if (!entry) return [];
  const key = collection === "files" ? entry.path : entry.destination;
  const candidate = path.resolve(root, key);
  return entries.flatMap((other, otherIndex) => {
    const otherKey = collection === "files" ? other.path : other.destination;
    return installTransactionPathsAlias(candidate, path.resolve(root, otherKey)) ? [otherIndex] : [];
  });
}

function assertInstallTransactionHostRecoveryObserved(operation) {
  const current = projectSkillHostDestinationSnapshot(operation.destination);
  if (projectSkillHostSnapshotsEqual(current, operation.observedState)) return true;
  if (projectSkillHostSnapshotsEqual(current, operation.originalState)) return false;
  throw new Error(`blocked: install transaction host destination changed during recovery: ${operation.destination}`);
}

function assertInstallTransactionFileRecoveryObserved(operation) {
  const current = installTransactionPathState(
    operation.destination,
    operation.kind,
    "critical recovery destination",
  );
  if (installTransactionStatesEqual(current, operation.observedState)) return true;
  if (installTransactionStatesEqual(current, operation.originalState)) return false;
  throw new Error(`blocked: install transaction destination changed during recovery: ${operation.destination}`);
}

function stageInstallTransactionRecoveryPath(source, scratchRoot, label, kind, expectedState) {
  if (!source) {
    throw new Error(`blocked: install transaction recovery source is missing: ${label}`);
  }
  const staged = path.join(
    scratchRoot,
    `.recovery-stage-${label}-${crypto.randomBytes(6).toString("hex")}`,
  );
  const metadata = fs.lstatSync(source);
  if (kind === "file") {
    if (metadata.isSymbolicLink() || !metadata.isFile()) {
      throw new Error(`blocked: install transaction recovery file is unsafe: ${source}`);
    }
    fs.copyFileSync(source, staged, fs.constants.COPYFILE_EXCL);
    fs.chmodSync(staged, metadata.mode & 0o777);
    assertInstallTransactionRecoveryPathState(staged, kind, expectedState, label);
    return staged;
  }
  if (kind === "directory") {
    copyTreeBinarySafe(source, staged);
    assertInstallTransactionRecoveryPathState(staged, kind, expectedState, label);
    return staged;
  }
  if (kind === "host") {
    if (metadata.isSymbolicLink()) {
      fs.symlinkSync(fs.readlinkSync(source), staged, "dir");
      assertInstallTransactionRecoveryPathState(staged, kind, expectedState, label);
      return staged;
    }
    if (metadata.isDirectory()) {
      copyTreeBinarySafe(source, staged);
      assertInstallTransactionRecoveryPathState(staged, kind, expectedState, label);
      return staged;
    }
  }
  throw new Error(`blocked: install transaction recovery source is unsafe: ${source}`);
}

function assertInstallTransactionRecoveryPathState(candidate, kind, expectedState, label) {
  const actual = kind === "host"
    ? projectSkillHostDestinationSnapshot(candidate)
    : installTransactionPathState(candidate, kind, "install transaction staged recovery path");
  const matches = kind === "host"
    ? projectSkillHostSnapshotsEqual(actual, expectedState)
    : installTransactionStatesEqual(actual, expectedState);
  if (!matches) {
    throw new Error(`blocked: install transaction recovery backup changed while staging: ${label}`);
  }
}

function assertInstallTransactionRecoveryBackupObserved(operation, kind) {
  if (!operation.backup) {
    throw new Error(`blocked: install transaction recovery backup is missing: ${operation.destination}`);
  }
  assertInstallTransactionRecoveryPathState(
    operation.backup,
    kind,
    operation.originalState,
    operation.destination,
  );
}

function moveInstallTransactionRecoveryDestinationAside(destination, scratchRoot, label) {
  if (!lstatIfExists(destination)) return;
  const retained = path.join(
    scratchRoot,
    `.recovery-previous-${label}-${crypto.randomBytes(6).toString("hex")}`,
  );
  fs.renameSync(destination, retained);
  const crashSuffix = process.env.AGENT_FLOW_TEST_CRASH_RECOVERY_DESTINATION_SUFFIX;
  if (
    process.env.AGENT_FLOW_TEST_CRASH_DURING_RECOVERY_AFTER_DESTINATION_MOVE === "1"
    || (typeof crashSuffix === "string" && crashSuffix && destination.endsWith(crashSuffix))
  ) {
    process.exit(80);
  }
}

function recoverUnpublishedInstallTransaction(root, transactionRoot, transactionMetadata) {
  const ownerPath = path.join(transactionRoot, "owner.json");
  const ownerMetadata = lstatIfExists(ownerPath);
  if (!ownerMetadata) {
    const ageMs = Date.now() - transactionMetadata.mtimeMs;
    if (ageMs < 5_000) {
      throw new Error("blocked: install transaction initialization is still in progress");
    }
    fs.rmSync(transactionRoot, { recursive: true, force: true });
    return;
  }
  const owner = readValidatedInstallTransactionOwner(root, transactionRoot);
  if (processIsAlive(owner.pid)) {
    throw new Error(`blocked: install transaction is active in process ${owner.pid}`);
  }
  fs.rmSync(transactionRoot, { recursive: true, force: true });
}

function readValidatedInstallTransactionOwner(root, transactionRoot) {
  const ownerPath = path.join(transactionRoot, "owner.json");
  const owner = readStrictInstalledObject(root, ownerPath, "install transaction owner");
  if (
    !hasExactObjectKeys(owner, ["created_at", "pid", "token", "version"])
    || owner.version !== INSTALL_TRANSACTION_OWNER_VERSION
    || !Number.isSafeInteger(owner.pid)
    || owner.pid <= 0
    || typeof owner.token !== "string"
    || !/^[0-9a-f]{32}$/.test(owner.token)
    || typeof owner.created_at !== "string"
    || Number.isNaN(Date.parse(owner.created_at))
  ) {
    throw new Error("blocked: install transaction owner is invalid");
  }
  return owner;
}

function processIsAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error?.code === "EPERM";
  }
}

function preflightInterruptedInstallRecovery(
  root,
  agentFlowDir,
  journal,
  { committed = false } = {},
) {
  const transactionRoot = installTransactionRoot(agentFlowDir);
  const staticSpecs = criticalInstallPaths(root, agentFlowDir);
  const staticPaths = new Set(staticSpecs.map((spec) => installTransactionRelative(root, spec.path)));
  const dynamicPaths = Array.isArray(journal.files)
    ? journal.files
        .map((entry) => entry?.path)
        .filter((relative) => typeof relative === "string" && !staticPaths.has(relative))
    : [];
  const expectedSpecs = new Map(criticalInstallPaths(root, agentFlowDir, dynamicPaths).map((spec, index) => [
    installTransactionRelative(root, spec.path),
    { ...spec, index },
  ]));
  if (journal.files.length !== expectedSpecs.size) {
    throw new Error("blocked: install transaction file journal is incomplete");
  }
  const files = [];
  const seenFiles = new Set();
  for (let fileIndex = 0; fileIndex < journal.files.length; fileIndex += 1) {
    const entry = journal.files[fileIndex];
    if (
      !entry
      || typeof entry !== "object"
      || !hasExactObjectKeys(entry, [
        "applied_state", "backup", "backup_sha256", "backup_tree_hash", "existed",
        "kind", "mode", "path", "pending_state", "recovery_state",
      ])
      || typeof entry.path !== "string"
      || !["file", "directory"].includes(entry.kind)
      || typeof entry.existed !== "boolean"
      || ![null, "restore-intent"].includes(entry.recovery_state)
      || (committed && (entry.pending_state !== null || entry.recovery_state !== null))
      || (entry.existed && (!Number.isInteger(entry.mode) || entry.mode < 0 || entry.mode > 0o777))
      || (!entry.existed && (
        entry.backup !== null
        || entry.mode !== null
        || entry.backup_sha256 !== null
        || entry.backup_tree_hash !== null
      ))
      || seenFiles.has(entry.path)
    ) {
      throw new Error("blocked: install transaction file journal is invalid");
    }
    seenFiles.add(entry.path);
    const spec = expectedSpecs.get(entry.path);
    if (!spec || spec.kind !== entry.kind) {
      throw new Error("blocked: install transaction file journal is noncanonical");
    }
    const destination = resolveInstallTransactionPath(root, entry.path, "file destination");
    assertInstallTransactionParentSafe(root, destination, "file destination");
    const appliedState = validateInstallTransactionState(entry.applied_state, entry.kind, true);
    const pendingState = entry.pending_state === null
      ? null
      : validateInstallTransactionState(entry.pending_state, entry.kind, true);
    let originalState = { kind: "absent" };
    let backup = null;
    let backupMissing = false;
    if (entry.existed) {
      if (
        (entry.kind === "file" && (
          typeof entry.backup_sha256 !== "string"
          || !/^[0-9a-f]{64}$/.test(entry.backup_sha256)
          || entry.backup_tree_hash !== null
        ))
        || (entry.kind === "directory" && (
          typeof entry.backup_tree_hash !== "string"
          || !/^[0-9a-f]{64}$/.test(entry.backup_tree_hash)
          || entry.backup_sha256 !== null
        ))
      ) {
        throw new Error("blocked: install transaction backup integrity is invalid");
      }
      const expectedBackup = path.join(
        transactionRoot,
        "files",
        entry.kind === "file" ? `${spec.index}.bin` : `${spec.index}.tree`,
      );
      backup = resolveInstallTransactionBackupPath(
        root,
        entry.backup,
        transactionRoot,
        expectedBackup,
        "file backup",
        { allowMissingParent: committed },
      );
      const recordedIntegrity = entry.kind === "file" ? entry.backup_sha256 : entry.backup_tree_hash;
      originalState = entry.kind === "file"
        ? { kind: "file", mode: entry.mode, sha256: recordedIntegrity }
        : { kind: "directory", mode: entry.mode, tree_hash: recordedIntegrity };
      if (lstatIfExists(backup)) {
        const backupState = requireInstallTransactionBackup(backup, entry.kind);
        const actualIntegrity = entry.kind === "file" ? backupState.sha256 : backupState.tree_hash;
        if (backupState.mode !== entry.mode || actualIntegrity !== recordedIntegrity) {
          throw new Error(`blocked: interrupted install ${entry.kind} backup changed: ${entry.path}`);
        }
      } else {
        backupMissing = true;
        backup = null;
      }
    }
    const currentState = installTransactionPathState(destination, entry.kind, "critical recovery destination");
    const matchesOriginal = installTransactionStatesEqual(currentState, originalState);
    const matchesApplied = installTransactionStatesEqual(currentState, appliedState);
    const matchesPending = pendingState !== null && installTransactionStatesEqual(currentState, pendingState);
    const recoveryIntermediate = entry.recovery_state === "restore-intent"
      && entry.existed
      && backup !== null
      && currentState.kind === "absent";
    if (backupMissing && !matchesOriginal && !committed) {
      throw new Error(`blocked: interrupted install ${entry.kind} backup is missing: ${entry.path}`);
    }
    if (!matchesOriginal && !matchesApplied && !matchesPending && !recoveryIntermediate) {
      throw new Error(`blocked: interrupted install destination changed after crash: ${entry.path}`);
    }
    if (committed && !matchesApplied) {
      throw new Error(`blocked: committed install destination does not match applied state: ${entry.path}`);
    }
    files.push({
      destination,
      existed: entry.existed,
      backup,
      mode: entry.mode,
      kind: entry.kind,
      observedState: currentState,
      originalState,
      journalIndex: fileIndex,
      restore: !matchesOriginal && (matchesApplied || matchesPending || recoveryIntermediate),
    });
  }

  const hosts = [];
  const seenHosts = new Set();
  for (let hostIndex = 0; hostIndex < journal.hosts.length; hostIndex += 1) {
    const entry = journal.hosts[hostIndex];
    if (
      !entry
      || typeof entry !== "object"
      || !hasExactObjectKeys(entry, [
        "action", "backup", "destination", "expected_kind", "expected_target",
        "expected_tree_hash", "expected_tree_integrity", "host", "name", "progress", "recovery_state",
        "source", "source_tree_hash", "source_tree_integrity",
      ])
      || typeof entry.name !== "string"
      || !isPortableSkillName(entry.name)
      || entry.name.length > 128
      || typeof entry.host !== "string"
      || ![...PROJECT_SKILL_HOSTS, "gemini", "antigravity"].includes(entry.host)
      || !["create", "replace", "delete"].includes(entry.action)
      || !["absent", "symlink", "directory"].includes(entry.expected_kind)
      || !["pending", "backup-intent", "backed-up", "apply-intent", "applied"].includes(entry.progress)
      || ![null, "restore-intent"].includes(entry.recovery_state)
      || (committed && (entry.progress !== "applied" || entry.recovery_state !== null))
      || typeof entry.destination !== "string"
      || seenHosts.has(entry.destination)
    ) {
      throw new Error("blocked: install transaction host journal is invalid");
    }
    seenHosts.add(entry.destination);
    const destination = resolveInstallTransactionPath(root, entry.destination, "host destination");
    assertInstallTransactionParentSafe(root, destination, "host destination");
    const canonicalHostRoot = installTransactionCanonicalHostRoot(root, entry);
    if (!canonicalHostRoot || path.resolve(destination) !== path.resolve(canonicalHostRoot, entry.name)) {
      throw new Error("blocked: install transaction host destination is noncanonical");
    }
    const expected = transactionExpectedHostSnapshot(entry);
    validateInstallTransactionHostAction(root, agentFlowDir, destination, entry, expected);
    const current = projectSkillHostDestinationSnapshot(destination);
    const expectedBackup = path.join(transactionRoot, "hosts", `${hostIndex}.backup`);
    const backup = entry.backup === null ? null : resolveInstallTransactionBackupPath(
      root,
      entry.backup,
      transactionRoot,
      expectedBackup,
      "host backup",
      { allowMissingParent: committed },
    );
    const backupExists = Boolean(backup && lstatIfExists(backup));
    if (backupExists) {
      const backupSnapshot = projectSkillHostDestinationSnapshot(backup);
      if (!projectSkillHostSnapshotsEqual(backupSnapshot, expected)) {
        throw new Error(`blocked: interrupted install host backup changed: ${entry.destination}`);
      }
    }
    if (committed) {
      if (!transactionHostSnapshotMatchesApplied(root, agentFlowDir, destination, current, entry)) {
        throw new Error(`blocked: committed install host destination is not applied: ${entry.destination}`);
      }
      hosts.push({
        destination,
        backup: backupExists ? backup : null,
        observedState: current,
        originalState: expected,
        journalIndex: hostIndex,
        restoreBackup: false,
        removeApplied: false,
      });
      continue;
    }
    if (backupExists) {
      if (projectSkillHostSnapshotsEqual(current, expected)) {
        hosts.push({ destination, backup, observedState: current, originalState: expected, journalIndex: hostIndex, restoreBackup: false, removeApplied: false });
        continue;
      }
      const matchesApplied = transactionHostSnapshotMatchesApplied(root, agentFlowDir, destination, current, entry);
      const authenticIntermediate = entry.action === "replace"
        && current.kind === "absent"
        && (
          ["backup-intent", "backed-up", "apply-intent"].includes(entry.progress)
          || entry.recovery_state === "restore-intent"
        );
      if (!matchesApplied && !authenticIntermediate) {
        throw new Error(`blocked: interrupted install host destination changed: ${entry.destination}`);
      }
      hosts.push({ destination, backup, observedState: current, originalState: expected, journalIndex: hostIndex, restoreBackup: true, removeApplied: false });
      continue;
    }
    if (entry.action !== "create") {
      if (!projectSkillHostSnapshotsEqual(current, expected)) {
        throw new Error(`blocked: interrupted install host backup is missing: ${entry.destination}`);
      }
      hosts.push({ destination, backup: null, observedState: current, originalState: expected, journalIndex: hostIndex, restoreBackup: false, removeApplied: false });
      continue;
    }
    if (
      current.kind !== "absent"
      && !transactionHostSnapshotMatchesApplied(root, agentFlowDir, destination, current, entry)
    ) {
      throw new Error(`blocked: interrupted install host destination changed: ${entry.destination}`);
    }
    if (current.kind !== "absent" && !["apply-intent", "applied"].includes(entry.progress)) {
      throw new Error(`blocked: interrupted install host progress is inconsistent: ${entry.destination}`);
    }
    hosts.push({
      destination,
      backup: null,
      observedState: current,
      originalState: expected,
      journalIndex: hostIndex,
      restoreBackup: false,
      removeApplied: current.kind !== "absent",
    });
  }
  return { files, hosts };
}

function assertCommittedInstallFinalState(root) {
  const kit = readInstalledKit(root);
  const index = readInstalledSkillIndex(root);
  const computedSkillPlanHash = computeSkillPlanHash(index, root, true);
  if (
    kit.skill_plan_hash_version !== 2
    || typeof kit.skill_plan_hash !== "string"
    || kit.skill_plan_hash !== computedSkillPlanHash
  ) {
    throw new Error("blocked: committed install skill plan does not match kit provenance");
  }
  if (
    kit.skill_links_commitment_version !== SKILL_LINKS_COMMITMENT_VERSION
    || typeof kit.skill_links_commitment !== "string"
    || !/^[0-9a-f]{64}$/.test(kit.skill_links_commitment)
  ) {
    throw new Error("blocked: committed install skill link commitment is invalid");
  }
  const links = authenticatedPreviousSkillLinks(kit, index);
  try {
    assertProjectRuntimeInstalled(root, kit);
  } catch (error) {
    throw new Error(`blocked: committed project runtime is invalid: ${error instanceof Error ? error.message : String(error)}`);
  }
  assertInstalledProfileSelection(root, kit, index);
  assertManagedHostFilesInstalled(root, kit);
  assertManagedHookContractInstalled(root, kit);
  assertCommittedSkillHostLinksApplied(root, index, links);
}

function validatedCodexTrustObligation(root, value) {
  if (value === null) return null;
  if (
    !value
    || typeof value !== "object"
    || Array.isArray(value)
    || !hasExactObjectKeys(value, [
      "config_path",
      "managed_hook_contract_commitment",
      "skill_plan_hash",
      "updates",
      "version",
    ])
    || value.version !== CODEX_TRUST_OBLIGATION_VERSION
    || typeof value.config_path !== "string"
    || !path.isAbsolute(value.config_path)
    || canonicalCodexConfigPath(value.config_path) !== value.config_path
    || typeof value.managed_hook_contract_commitment !== "string"
    || !/^[0-9a-f]{64}$/.test(value.managed_hook_contract_commitment)
    || typeof value.skill_plan_hash !== "string"
    || !/^[0-9a-f]{64}$/.test(value.skill_plan_hash)
    || !Array.isArray(value.updates)
    || value.updates.length < 2
  ) {
    throw new Error("blocked: install transaction Codex trust obligation is invalid");
  }
  const canonicalRoot = fs.realpathSync.native(root);
  const installed = readInstalledKit(root);
  const configuredPath = codexConfigPath();
  if (
    !configuredPath
    || canonicalCodexConfigPath(configuredPath) !== value.config_path
    || installed.managed_hook_contract_commitment !== value.managed_hook_contract_commitment
    || installed.skill_plan_hash !== value.skill_plan_hash
  ) {
    throw new Error("blocked: install transaction Codex trust obligation is not bound to the installed project");
  }
  const seenHooks = new Set();
  const updates = value.updates.map((update, index) => {
    if (
      !update
      || typeof update !== "object"
      || Array.isArray(update)
      || !hasExactObjectKeys(update, [
        "expectedValue", "key", "tableHeader", "tablePath", "value",
      ])
      || typeof update.tableHeader !== "string"
      || !Array.isArray(update.tablePath)
      || typeof update.key !== "string"
      || typeof update.value !== "string"
      || typeof update.expectedValue !== "string"
    ) {
      throw new Error("blocked: install transaction Codex trust obligation is invalid");
    }
    if (index === 0) {
      if (
        update.tableHeader !== `[projects."${tomlBasicString(canonicalRoot)}"]`
        || JSON.stringify(update.tablePath) !== JSON.stringify(["projects", canonicalRoot])
        || update.key !== "trust_level"
        || update.value !== "\"trusted\""
        || update.expectedValue !== "trusted"
      ) {
        throw new Error("blocked: install transaction Codex trust obligation is invalid");
      }
    } else {
      const hookKey = update.tablePath[2];
      if (
        update.tablePath.length !== 3
        || update.tablePath[0] !== "hooks"
        || update.tablePath[1] !== "state"
        || typeof hookKey !== "string"
        || !hookKey
        || seenHooks.has(hookKey)
        || update.tableHeader !== `[hooks.state."${tomlBasicString(hookKey)}"]`
        || update.key !== "trusted_hash"
        || !/^sha256:[0-9a-f]{64}$/.test(update.expectedValue)
        || update.value !== `"${tomlBasicString(update.expectedValue)}"`
      ) {
        throw new Error("blocked: install transaction Codex trust obligation is invalid");
      }
      seenHooks.add(hookKey);
    }
    return {
      tableHeader: update.tableHeader,
      tablePath: [...update.tablePath],
      key: update.key,
      value: update.value,
      expectedValue: update.expectedValue,
    };
  });
  return {
    version: value.version,
    configPath: value.config_path,
    managedHookContractCommitment: value.managed_hook_contract_commitment,
    skillPlanHash: value.skill_plan_hash,
    updates,
  };
}

function installTransactionCommitProof(root, journal) {
  const kitPath = path.join(root, ".agent-flow", "kit.json");
  requireInstalledRegularFile(root, kitPath, "install transaction commit kit");
  const kitBytes = fs.readFileSync(kitPath);
  let kit;
  try {
    kit = JSON.parse(kitBytes.toString("utf8"));
  } catch (error) {
    throw new Error(`blocked: install transaction commit kit is unreadable: ${error.message}`);
  }
  const files = journal.files.map((entry) => [
    entry.path,
    entry.kind,
    entry.existed,
    entry.backup,
    entry.mode,
    entry.backup_sha256,
    entry.backup_tree_hash,
    normalizedInstallTransactionProofState(entry.applied_state),
    entry.pending_state === null ? null : normalizedInstallTransactionProofState(entry.pending_state),
    entry.recovery_state,
  ]);
  const hosts = journal.hosts.map((entry) => [
    entry.name,
    entry.host,
    entry.destination,
    entry.action,
    entry.expected_kind,
    entry.expected_target,
    entry.expected_tree_hash,
    entry.expected_tree_integrity,
    entry.backup,
    entry.source,
    entry.source_tree_hash,
    entry.source_tree_integrity,
    entry.progress,
    entry.recovery_state,
  ]);
  const codexTrustPlan = validatedCodexTrustObligation(root, journal.codex_trust);
  const codexTrust = codexTrustPlan === null ? null : [
    codexTrustPlan.version,
    codexTrustPlan.configPath,
    codexTrustPlan.managedHookContractCommitment,
    codexTrustPlan.skillPlanHash,
    codexTrustPlan.updates.map((update) => [
      update.tableHeader,
      update.tablePath,
      update.key,
      update.value,
      update.expectedValue,
    ]),
  ];
  return sha256Bytes(Buffer.from(JSON.stringify({
    version: INSTALL_TRANSACTION_COMMIT_PROOF_VERSION,
    kit_sha256: sha256Bytes(kitBytes),
    skill_plan_hash: kit?.skill_plan_hash ?? null,
    skill_links_commitment: kit?.skill_links_commitment ?? null,
    managed_host_files_commitment: kit?.managed_host_files_commitment ?? null,
    managed_hook_contract_commitment: kit?.managed_hook_contract_commitment ?? null,
    project_runtime_contract_commitment: kit?.project_runtime_contract_commitment ?? null,
    codex_trust: codexTrust,
    files,
    hosts,
  }), "utf8"));
}

function normalizedInstallTransactionProofState(state) {
  if (!state || typeof state !== "object" || Array.isArray(state)) return null;
  if (state.kind === "absent") return ["absent"];
  if (state.kind === "file") return ["file", state.mode, state.sha256];
  if (state.kind === "directory") return ["directory", state.mode, state.tree_hash];
  return null;
}

function assertCommittedSkillHostLinksApplied(root, index, links) {
  for (const link of links) {
    const destination = path.resolve(root, link.path);
    ensureChildPath(root, destination);
    if (pathHasSymlink(root, path.dirname(destination))) {
      throw new Error(`blocked: committed skill link parent is unsafe: ${link.path}`);
    }
    const snapshot = projectSkillHostDestinationSnapshot(destination);
    if (["removed-stale-linked", "removed-stale-copied"].includes(link.status)) {
      if (snapshot.kind !== "absent") {
        throw new Error(`blocked: committed stale skill link still exists: ${link.path}`);
      }
      continue;
    }
    const skill = index.skills.find((candidate) => candidate?.name === link.name);
    if (!skill || typeof skill.path !== "string") {
      throw new Error(`blocked: committed skill link has no indexed source: ${link.name}`);
    }
    const source = path.dirname(path.resolve(root, skill.path));
    const sourceTreeHash = requireProjectSkillTreeHash(skill.tree_hash, link.name);
    const sourceTreeIntegrity = requireProjectSkillTreeHash(link.tree_integrity, link.name);
    if (
      hashSkillTree(source) !== sourceTreeHash
      || installTransactionTreeIntegrity(source) !== sourceTreeIntegrity
    ) {
      throw new Error(`blocked: committed skill link source changed: ${link.name}`);
    }
    if (link.status === "linked") {
      if (
        snapshot.kind !== "symlink"
        || snapshot.target !== path.relative(path.dirname(destination), source)
      ) {
        throw new Error(`blocked: committed skill symlink is not applied: ${link.path}`);
      }
      continue;
    }
    if (
      link.status !== "copied"
      || snapshot.kind !== "directory"
      || snapshot.treeHash !== sourceTreeHash
      || snapshot.integrityHash !== sourceTreeIntegrity
    ) {
      throw new Error(`blocked: committed skill copy is not applied: ${link.path}`);
    }
  }
}

function hasExactObjectKeys(value, expected) {
  const actual = Object.keys(value).sort();
  const canonical = [...expected].sort();
  return actual.length === canonical.length && actual.every((key, index) => key === canonical[index]);
}

function transactionExpectedHostSnapshot(entry) {
  if (entry.expected_kind === "absent") {
    if (
      entry.expected_target !== null
      || entry.expected_tree_hash !== null
      || entry.expected_tree_integrity !== null
    ) {
      throw new Error("blocked: install transaction host expected absence is invalid");
    }
    return { kind: "absent" };
  }
  if (entry.expected_kind === "symlink") {
    if (
      typeof entry.expected_target !== "string"
      || entry.expected_tree_hash !== null
      || entry.expected_tree_integrity !== null
    ) {
      throw new Error("blocked: install transaction host expected symlink is invalid");
    }
    return { kind: "symlink", target: entry.expected_target };
  }
  if (
    entry.expected_target !== null
    || typeof entry.expected_tree_hash !== "string"
    || !/^[0-9a-f]{64}$/.test(entry.expected_tree_hash)
    || typeof entry.expected_tree_integrity !== "string"
    || !/^[0-9a-f]{64}$/.test(entry.expected_tree_integrity)
  ) {
    throw new Error("blocked: install transaction host expected tree is invalid");
  }
  return {
    kind: "directory",
    treeHash: entry.expected_tree_hash,
    integrityHash: entry.expected_tree_integrity,
  };
}

function installTransactionCanonicalHostRoot(root, entry) {
  if (entry.action !== "delete") {
    return PROJECT_SKILL_HOSTS.includes(entry.host) ? hostSkillRoot(root, entry.host) : null;
  }
  const legacy = legacyHostSkillRoot(root, entry.destination);
  if (legacy) {
    const normalized = entry.destination.replaceAll("\\", "/");
    const expectedHost = normalized.startsWith(".gemini/antigravity/")
      ? "antigravity"
      : normalized.startsWith(".gemini/") ? "gemini" : "codex";
    return entry.host === expectedHost ? legacy : null;
  }
  return PROJECT_SKILL_HOSTS.includes(entry.host) ? hostSkillRoot(root, entry.host) : null;
}

function validateInstallTransactionHostAction(root, agentFlowDir, destination, entry, expected) {
  const createsOrReplaces = ["create", "replace"].includes(entry.action);
  if (
    (entry.action === "create" && (expected.kind !== "absent" || entry.backup !== null))
    || (entry.action === "replace" && (expected.kind === "absent" || typeof entry.backup !== "string"))
    || (entry.action === "delete" && (expected.kind === "absent" || typeof entry.backup !== "string"))
    || (createsOrReplaces && (
      typeof entry.source !== "string"
      || typeof entry.source_tree_hash !== "string"
      || !/^[0-9a-f]{64}$/.test(entry.source_tree_hash)
      || typeof entry.source_tree_integrity !== "string"
      || !/^[0-9a-f]{64}$/.test(entry.source_tree_integrity)
      || !PROJECT_SKILL_HOSTS.includes(entry.host)
    ))
    || (!createsOrReplaces && (
      entry.source !== null
      || entry.source_tree_hash !== null
      || entry.source_tree_integrity !== null
    ))
  ) {
    throw new Error("blocked: install transaction host action is invalid");
  }
  const canonicalSources = installTransactionCanonicalSkillSources(root, agentFlowDir, entry.name);
  if (createsOrReplaces) {
    const source = resolveInstallTransactionPath(root, entry.source, "host source");
    if (!canonicalSources.includes(path.resolve(source))) {
      throw new Error("blocked: install transaction host source is noncanonical");
    }
  }
  if (expected.kind === "symlink") {
    const target = path.resolve(path.dirname(destination), expected.target);
    if (!canonicalSources.includes(target)) {
      throw new Error("blocked: install transaction host expected symlink is noncanonical");
    }
  }
}

function installTransactionCanonicalSkillSources(root, agentFlowDir, name) {
  return [
    path.join(agentFlowDir, "skills", name),
    path.join(agentFlowDir, "local-skills", name),
    path.join(root, "skills", name),
  ].map((candidate) => path.resolve(candidate));
}

function transactionHostSnapshotMatchesApplied(root, agentFlowDir, destination, current, entry) {
  if (entry.action === "delete") return current.kind === "absent";
  const source = resolveInstallTransactionPath(root, entry.source, "host source");
  if (!installTransactionCanonicalSkillSources(root, agentFlowDir, entry.name).includes(path.resolve(source))) {
    throw new Error("blocked: install transaction host source is noncanonical");
  }
  const sourceTreeHash = requireProjectSkillTreeHash(entry.source_tree_hash, entry.name);
  const sourceTreeIntegrity = requireProjectSkillTreeHash(entry.source_tree_integrity, entry.name);
  if (hashSkillTree(source) !== sourceTreeHash) {
    throw new Error(`blocked: interrupted install host source changed: ${entry.name}`);
  }
  if (installTransactionTreeIntegrity(source) !== sourceTreeIntegrity) {
    throw new Error(`blocked: interrupted install host source modes changed: ${entry.name}`);
  }
  return current.kind === "symlink"
    ? current.target === path.relative(path.dirname(destination), source)
    : current.kind === "directory"
      && current.treeHash === sourceTreeHash
      && current.integrityHash === sourceTreeIntegrity;
}

function requireInstallTransactionBackup(backup, kind) {
  const metadata = lstatIfExists(backup);
  const valid = metadata && !metadata.isSymbolicLink() && (
    kind === "file" ? metadata.isFile() : metadata.isDirectory()
  );
  if (!valid) {
    throw new Error(`blocked: install transaction ${kind} backup is missing or unsafe: ${backup}`);
  }
  return installTransactionPathState(backup, kind, "install transaction backup");
}

function assertInstallTransactionParentSafe(root, destination, label) {
  ensureChildPath(root, destination);
  const relative = path.relative(root, path.dirname(destination));
  let cursor = root;
  for (const part of relative.split(path.sep).filter(Boolean)) {
    cursor = path.join(cursor, part);
    const metadata = lstatIfExists(cursor);
    if (!metadata) break;
    if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
      throw new Error(`blocked: install transaction ${label} parent is unsafe`);
    }
  }
}

function resolveInstallTransactionPath(root, relative, label) {
  if (typeof relative !== "string" || !relative || relative.includes("\\") || path.isAbsolute(relative)) {
    throw new Error(`blocked: invalid install transaction ${label}`);
  }
  const resolved = path.resolve(root, relative);
  ensureChildPath(root, resolved);
  return resolved;
}

function resolveInstallTransactionBackupPath(
  root,
  relative,
  transactionRoot,
  expectedPath,
  label,
  { allowMissingParent = false } = {},
) {
  const resolved = resolveInstallTransactionPath(root, relative, label);
  ensureChildPath(transactionRoot, resolved);
  if (path.resolve(resolved) !== path.resolve(expectedPath)) {
    throw new Error(`blocked: invalid install transaction ${label}`);
  }
  const parent = path.dirname(resolved);
  if (allowMissingParent && !lstatIfExists(parent)) {
    assertInstallTransactionParentSafe(root, parent, label);
  } else {
    assertExistingInstallTransactionDirectoryChain(root, transactionRoot, parent, label);
  }
  return resolved;
}

function assertExistingInstallTransactionDirectoryChain(root, transactionRoot, target, label) {
  ensureChildPath(root, transactionRoot);
  ensureChildPath(transactionRoot, target);
  const transactionMetadata = lstatIfExists(transactionRoot);
  if (!transactionMetadata || transactionMetadata.isSymbolicLink() || !transactionMetadata.isDirectory()) {
    throw new Error(`blocked: install transaction ${label} root is unsafe`);
  }
  assertInstallTransactionParentSafe(root, transactionRoot, label);
  let cursor = transactionRoot;
  for (const part of path.relative(transactionRoot, target).split(path.sep).filter(Boolean)) {
    cursor = path.join(cursor, part);
    const metadata = lstatIfExists(cursor);
    if (!metadata || metadata.isSymbolicLink() || !metadata.isDirectory()) {
      throw new Error(`blocked: install transaction ${label} parent is unsafe`);
    }
  }
}

function installTransactionPathState(candidate, expectedKind, label) {
  const metadata = lstatIfExists(candidate);
  if (!metadata) return { kind: "absent" };
  if (metadata.isSymbolicLink()) {
    throw new Error(`blocked: ${label} may not be a symlink: ${candidate}`);
  }
  const mode = metadata.mode & 0o777;
  if (metadata.isFile()) {
    if (expectedKind && expectedKind !== "file") {
      throw new Error(`blocked: ${label} has the wrong kind: ${candidate}`);
    }
    return {
      kind: "file",
      mode,
      sha256: crypto.createHash("sha256").update(fs.readFileSync(candidate)).digest("hex"),
    };
  }
  if (metadata.isDirectory()) {
    if (expectedKind && expectedKind !== "directory") {
      throw new Error(`blocked: ${label} has the wrong kind: ${candidate}`);
    }
    return {
      kind: "directory",
      mode,
      tree_hash: installTransactionTreeIntegrity(candidate),
    };
  }
  throw new Error(`blocked: ${label} may not be a special file: ${candidate}`);
}

function installTransactionTreeIntegrity(treeRoot) {
  return installTransactionTreeIntegrityFromEntries(installTransactionTreeEntries(treeRoot));
}

function installTransactionTreeEntries(treeRoot) {
  const entries = [];
  function walk(current, relative) {
    const metadata = fs.lstatSync(current);
    if (metadata.isSymbolicLink()) {
      throw new Error(`blocked: install transaction tree contains a symlink: ${current}`);
    }
    if (!metadata.isDirectory()) {
      throw new Error(`blocked: install transaction tree root is not a directory: ${current}`);
    }
    entries.push({ path: relative, type: "directory", mode: metadata.mode & 0o777 });
    for (const name of fs.readdirSync(current).sort()) {
      const child = path.join(current, name);
      const childRelative = relative ? `${relative}/${name}` : name;
      const childMetadata = fs.lstatSync(child);
      if (childMetadata.isSymbolicLink()) {
        throw new Error(`blocked: install transaction tree contains a symlink: ${child}`);
      }
      if (childMetadata.isDirectory()) {
        walk(child, childRelative);
      } else if (childMetadata.isFile()) {
        entries.push({
          path: childRelative,
          type: "file",
          mode: childMetadata.mode & 0o777,
          sha256: crypto.createHash("sha256").update(fs.readFileSync(child)).digest("hex"),
        });
      } else {
        throw new Error(`blocked: install transaction tree contains a special file: ${child}`);
      }
    }
  }
  walk(treeRoot, "");
  entries.sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
  return entries;
}

function installTransactionTreeIntegrityFromEntries(entries) {
  return crypto.createHash("sha256").update(JSON.stringify({ version: 1, entries })).digest("hex");
}

function emptyInstallTransactionTreeIntegrity(mode) {
  return crypto.createHash("sha256").update(JSON.stringify({
    version: 1,
    entries: [{ path: "", type: "directory", mode }],
  })).digest("hex");
}

function validateInstallTransactionState(state, expectedKind, allowAbsent) {
  if (!state || typeof state !== "object" || Array.isArray(state)) {
    throw new Error("blocked: install transaction applied state is invalid");
  }
  if (state.kind === "absent") {
    if (!allowAbsent || !hasExactObjectKeys(state, ["kind"])) {
      throw new Error("blocked: install transaction applied state is invalid");
    }
    return state;
  }
  if (state.kind === "file") {
    if (
      expectedKind !== "file"
      || !hasExactObjectKeys(state, ["kind", "mode", "sha256"])
      || !Number.isInteger(state.mode)
      || state.mode < 0
      || state.mode > 0o777
      || typeof state.sha256 !== "string"
      || !/^[0-9a-f]{64}$/.test(state.sha256)
    ) {
      throw new Error("blocked: install transaction applied file state is invalid");
    }
    return state;
  }
  if (
    state.kind !== "directory"
    || expectedKind !== "directory"
    || !hasExactObjectKeys(state, ["kind", "mode", "tree_hash"])
    || !Number.isInteger(state.mode)
    || state.mode < 0
    || state.mode > 0o777
    || typeof state.tree_hash !== "string"
    || !/^[0-9a-f]{64}$/.test(state.tree_hash)
  ) {
    throw new Error("blocked: install transaction applied directory state is invalid");
  }
  return state;
}

function installTransactionStatesEqual(left, right) {
  if (left.kind !== right.kind) return false;
  if (left.kind === "absent") return true;
  if (left.mode !== right.mode) return false;
  return left.kind === "file" ? left.sha256 === right.sha256 : left.tree_hash === right.tree_hash;
}

function removeOwnedInstallTransaction(transactionRoot, ownedIdentity, ownerToken) {
  removeValidatedInstallTransaction(transactionRoot, ownedIdentity, {
    token: ownerToken,
    pid: process.pid,
  });
}

function detachValidatedInstallTransaction(transactionRoot, ownedIdentity, expectedOwner) {
  if (!ownedIdentity) return null;
  const metadata = lstatIfExists(transactionRoot);
  if (
    !metadata
    || metadata.isSymbolicLink()
    || !metadata.isDirectory()
    || metadata.dev !== ownedIdentity.dev
    || metadata.ino !== ownedIdentity.ino
  ) {
    return null;
  }
  const ownerPath = path.join(transactionRoot, "owner.json");
  const ownerMetadata = lstatIfExists(ownerPath);
  if (expectedOwner) {
    if (!ownerMetadata || ownerMetadata.isSymbolicLink() || !ownerMetadata.isFile()) return null;
    let owner;
    try {
      owner = JSON.parse(fs.readFileSync(ownerPath, "utf8"));
    } catch {
      return null;
    }
    if (owner.token !== expectedOwner.token || owner.pid !== expectedOwner.pid) return null;
  } else if (ownerMetadata) {
    return null;
  }
  const parent = path.dirname(transactionRoot);
  let cleanupRoot;
  do {
    cleanupRoot = path.join(
      parent,
      `.install-transaction-cleanup-${process.pid}-${crypto.randomBytes(6).toString("hex")}`,
    );
  } while (lstatIfExists(cleanupRoot));
  fs.renameSync(transactionRoot, cleanupRoot);
  const descriptor = fs.openSync(parent, "r");
  try {
    fs.fsyncSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
  return cleanupRoot;
}

function removeValidatedInstallTransaction(
  transactionRoot,
  ownedIdentity,
  expectedOwner,
  { requireOwner = false } = {},
) {
  if (!ownedIdentity) return;
  const metadata = lstatIfExists(transactionRoot);
  if (
    !metadata
    || metadata.isSymbolicLink()
    || !metadata.isDirectory()
    || metadata.dev !== ownedIdentity.dev
    || metadata.ino !== ownedIdentity.ino
  ) {
    return;
  }
  const ownerPath = path.join(transactionRoot, "owner.json");
  const ownerMetadata = lstatIfExists(ownerPath);
  if (!ownerMetadata && requireOwner) return;
  if (ownerMetadata) {
    if (ownerMetadata.isSymbolicLink() || !ownerMetadata.isFile()) return;
    try {
      const owner = JSON.parse(fs.readFileSync(ownerPath, "utf8"));
      if (owner.token !== expectedOwner.token || owner.pid !== expectedOwner.pid) return;
    } catch {
      return;
    }
  }
  fs.rmSync(transactionRoot, { recursive: true, force: true });
}

function preflightInstallSkillSources(root, agentFlowDir, installSelection) {
  const allowed = installSelection?.skillNames || null;
  const localBase = path.join(agentFlowDir, "local-skills");
  if (lstatIfExists(localBase)) {
    requireProjectLocalDirectory(root, localBase, "project-local skill root");
  }
  const discovered = [
    ...discoverSkills(localBase, "local", root, new Set(), allowed),
  ];
  if (!samePath(root, KIT_ROOT)) {
    const projectBase = path.join(root, "skills");
    if (lstatIfExists(projectBase)) {
      requireProjectLocalDirectory(root, projectBase, "project-local skill root");
    }
    discovered.push(...discoverSkills(projectBase, "project", root, new Set(), allowed));
  }
  discovered.push(...discoverSkills(
    path.join(KIT_ROOT, "skills"),
    "bundled",
    KIT_ROOT,
    new Set(["index.json"]),
    allowed,
  ));
  selectCanonicalProjectSkills(discovered);
}

function selectProjectSkills(root, agentFlowDir, installSelection = null, skillPlan = null) {
  const discovered = [
    ...discoverSkills(path.join(agentFlowDir, "local-skills"), "local", root),
    ...discoverProjectSkills(root),
    ...discoverSkills(
      path.join(agentFlowDir, "skills"),
      "bundled",
      root,
      new Set(["index.json"]),
    ),
  ];
  const byName = selectCanonicalProjectSkills(discovered);
  const warnings = [];
  for (const skill of discovered) {
    warnings.push(...skill.warnings);
  }
  const allowed = installSelection?.skillNames || null;
  const sourceByName = indexSkillEntriesByLogicalName(skillPlan?.entries || [], "resolved skill source plan");
  const skills = [...byName.values()]
    .filter((skill) => !allowed || allowed.has(skill.name))
    .map((skill) => {
      const origin = sourceByName.get(portableSkillCasefold(skill.name));
      if (!origin || skill.source !== "bundled") return skill;
      return {
        ...skill,
        discovery_source: skill.source,
        source: origin.source_kind,
        source_host: origin.source_host,
        tree_hash: origin.tree_hash,
        ...(origin.automatic_on_demand ? {
          activation: "on-demand",
          workflowPhases: [],
          taskTerms: [],
          pathGlobs: [],
        } : {}),
      };
    })
    .map((skill) => ({
      ...skill,
      profiles: ["local", "project"].includes(skill.source)
        ? ["project"]
        : [...(installSelection?.skillProfiles || installSelection?.profiles || [])].sort(),
    }))
    .sort((a, b) => compareCodePoints(a.name, b.name));
  const indexedNames = new Set(skills.map((skill) => portableSkillCasefold(skill.name)));
  const missingResolved = [...sourceByName.keys()]
    .filter((name) => !indexedNames.has(name))
    .sort(compareCodePoints);
  if (missingResolved.length > 0) {
    throw new Error(
      `blocked: resolved skill sources are missing from the installed index: ${missingResolved.join(", ")}`,
    );
  }
  warnings.push(...validateSkillDependencies(skills));
  const externalExposureSkills = externalSkillExposureClosure(
    skills,
    installSelection?.explicitSkills || [],
    (skillPlan?.entries || [])
      .filter((entry) => entry.automatic_on_demand)
      .map((entry) => entry.name),
  );
  const conflicts = [];
  for (const skill of skills) {
    const canonicalName = portableSkillCasefold(skill.name);
    const ignored = discovered
      .filter((candidate) => (
        portableSkillCasefold(candidate.name) === canonicalName
        && candidate.path !== skill.path
      ))
      .sort((a, b) => a.priority - b.priority)
      .map((candidate) => candidate.path);
    if (ignored.length > 0) {
      conflicts.push({ name: skill.name, selected: skill.path, ignored });
    }
  }
  return {
    version: 2,
    selection: {
      mode: allowed ? "filtered" : "all",
      profile_selection: installSelection?.profileSelection || "auto",
      profiles: installSelection?.profiles || [],
      skill_profiles: installSelection?.skillProfiles || installSelection?.profiles || [],
      required_review: installSelection?.requiredReview || {},
      conditional_skills: installSelection?.conditionalSkills || {},
      profile_routing: installSelection?.profileRouting || {},
      explicit_skills: installSelection?.explicitSkills || [],
      external_exposure_skills: externalExposureSkills,
    },
    skills: skills.map(({ priority, warnings: _warnings, ...skill }) => skill),
    conflicts,
    warnings,
  };
}

function selectCanonicalProjectSkills(discovered) {
  const byName = new Map();
  for (const skill of discovered) {
    const canonicalName = portableSkillCasefold(skill.name);
    if (
      RESERVED_CORE_SKILL_NAMES.has(canonicalName)
      && ["local", "project"].includes(skill.source)
    ) {
      throw new Error(
        `reserved workflow skill name cannot be overridden: ${skill.name} (${skill.path})`,
      );
    }
    const current = byName.get(canonicalName);
    if (!current) {
      byName.set(canonicalName, skill);
      continue;
    }
    if (skill.priority !== current.priority) {
      if (skill.priority < current.priority) byName.set(canonicalName, skill);
      continue;
    }
    const currentPath = current.path.split(path.sep).join("/");
    const nextPath = skill.path.split(path.sep).join("/");
    if (currentPath !== nextPath) {
      throw new Error(
        `conflicting project-local skill paths for canonical name ${JSON.stringify(canonicalName)}: `
        + `${currentPath}; ${nextPath}`,
      );
    }
  }
  return byName;
}

function indexSkillEntriesByLogicalName(entries, label) {
  const byName = new Map();
  for (const entry of entries) {
    if (!isPortableSkillName(entry?.name)) {
      throw new Error(`blocked: invalid logical skill name in ${label}: ${JSON.stringify(entry?.name)}`);
    }
    const key = portableSkillCasefold(entry?.name);
    if (byName.has(key)) {
      throw new Error(`blocked: duplicate logical skill name in ${label}: ${JSON.stringify(entry?.name)}`);
    }
    byName.set(key, entry);
  }
  return byName;
}

function validateSkillDependencies(skills) {
  const names = new Set(skills.map((skill) => portableSkillCasefold(skill.name)));
  const warnings = [];
  for (const skill of skills) {
    for (const required of skill.requires || []) {
      if (!names.has(portableSkillCasefold(required))) {
        warnings.push(`${skill.name}: missing required skill ${required}`);
      }
    }
  }
  return warnings;
}

function externalSkillExposureClosure(skills, explicitSkills, automaticSkills = []) {
  const byName = indexSkillEntriesByLogicalName(skills, "installed skill exposure closure");
  const pending = [];
  for (const rawName of explicitSkills) {
    if (!isPortableSkillName(rawName)) {
      throw new Error(`blocked: invalid explicit external skill name: ${JSON.stringify(rawName)}`);
    }
    const name = portableSkillCasefold(rawName);
    if (byName.has(name)) pending.push(name);
  }
  for (const rawName of automaticSkills) {
    if (!isPortableSkillName(rawName)) {
      throw new Error(`blocked: invalid automatic external skill name: ${JSON.stringify(rawName)}`);
    }
    const name = portableSkillCasefold(rawName);
    if (byName.has(name)) pending.push(name);
  }
  for (const skill of skills) {
    const discoverySource = skill.discovery_source ?? skill.source;
    if (["local", "project"].includes(discoverySource)) {
      pending.push(portableSkillCasefold(skill.name));
    }
  }
  const visited = new Set();
  const exposed = new Set();
  while (pending.length > 0) {
    const name = pending.shift();
    if (visited.has(name)) continue;
    visited.add(name);
    const skill = byName.get(name);
    if (!skill) continue;
    if (["host-bootstrap", "shared"].includes(skill.source)) exposed.add(name);
    for (const rawDependency of [...(skill.dependencies || []), ...(skill.requires || [])]) {
      if (!isPortableSkillName(rawDependency)) {
        throw new Error(
          `blocked: invalid dependency in installed skill exposure closure: ${skill.name}`,
        );
      }
      const dependency = portableSkillCasefold(rawDependency);
      if (byName.has(dependency) && !visited.has(dependency)) pending.push(dependency);
    }
  }
  return [...exposed].sort(compareCodePoints);
}

function normalizedExternalExposureSkillNames(selection, { legacyFallback = true } = {}) {
  const hasExposure = Object.hasOwn(selection || {}, "external_exposure_skills");
  const rawNames = hasExposure
    ? selection.external_exposure_skills
    : legacyFallback
      ? selection?.explicit_skills || []
      : [];
  if (!Array.isArray(rawNames)) {
    throw new Error("blocked: installed external skill exposure list is invalid");
  }
  const names = [];
  const seen = new Set();
  for (const rawName of rawNames) {
    if (!isPortableSkillName(rawName)) {
      throw new Error(`blocked: invalid external skill exposure name: ${JSON.stringify(rawName)}`);
    }
    const name = portableSkillCasefold(rawName);
    if (seen.has(name)) {
      throw new Error(`blocked: duplicate external skill exposure name: ${rawName}`);
    }
    seen.add(name);
    names.push(name);
  }
  return names;
}

function discoverProjectSkills(root) {
  if (samePath(root, KIT_ROOT)) {
    return [];
  }
  return discoverSkills(path.join(root, "skills"), "project", root);
}

function discoverSkills(baseDir, source, root, ignoredNames = new Set(), allowedNames = null) {
  if (!fs.existsSync(baseDir)) {
    return [];
  }
  const priority = { local: 0, project: 1, bundled: 2 }[source] ?? 99;
  const skills = [];
  for (const entry of fs.readdirSync(baseDir, { withFileTypes: true })) {
    if (entry.isSymbolicLink()) {
      throw new Error(`skill source may not be a symlink: ${path.join(baseDir, entry.name)}`);
    }
    if (!entry.isDirectory() || ignoredNames.has(entry.name)) {
      continue;
    }
    const skillPath = path.join(baseDir, entry.name, "SKILL.md");
    if (!fs.existsSync(skillPath)) {
      continue;
    }
    const text = fs.readFileSync(skillPath, "utf8");
    const lineCount = text.split(/\r?\n/).length;
    if (lineCount > 200) {
      throw new Error(`${skillPath}: ${lineCount} lines; max is 200, split progressive references`);
    }
    const metadata = parseSkillMetadata(text, entry.name);
    if (allowedNames && !allowedNames.has(entry.name) && !allowedNames.has(metadata.name)) {
      continue;
    }
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
      ...(metadata.routingDeclared ? {
        activation: metadata.activation,
        taskTerms: metadata.taskTerms,
        pathGlobs: metadata.pathGlobs,
      } : {}),
      reviewAngles: metadata.reviewAngles,
      installGroup: metadata.installGroup,
      excludes: metadata.excludes,
      tags: metadata.tags,
      description: metadata.description,
      trigger: metadata.trigger,
      triggers: metadata.triggers,
      hash: crypto.createHash("sha256").update(text).digest("hex"),
      tree_hash: hashSkillTree(path.dirname(skillPath)),
      line_count: lineCount,
      context_risk: lineCount > 200 ? "high" : lineCount > 100 ? "split-recommended" : "normal",
      priority,
      warnings: [
        ...metadata.warnings.map((message) => `${relativePath}: ${message}`),
        ...(lineCount > 100 ? [`${relativePath}: ${lineCount} lines; split progressive references`] : []),
      ],
    });
  }
  return skills;
}

function parseSkillMetadata(text, fallbackName) {
  const frontmatter = splitSkillFrontmatter(text);
  const metadata = frontmatter ? parseSimpleYaml(frontmatter) : {};
  const warnings = [];
  const parsedName = String(metadata.name || fallbackName);
  if (!isPortableSkillName(parsedName)) {
    throw new Error(`unsafe project-local skill name: ${parsedName}`);
  }
  const name = parsedName;
  const hostValues = Array.isArray(metadata.hosts) ? metadata.hosts : [];
  const knownHosts = new Set(PROJECT_SKILL_HOSTS);
  for (const host of hostValues) {
    const normalized = String(host).trim().toLowerCase();
    if (!knownHosts.has(normalized) && normalized) {
      warnings.push(`unknown host ignored: ${normalized}`);
    }
  }
  const body = text.replace(/^---\n[\s\S]*?\n---\n?/, "");
  const useWhen = body.split(/\r?\n/).find((line) => /^\s*use when\b/i.test(line));
  const activation = metadata.activation === undefined || metadata.activation === null
    ? null
    : String(metadata.activation).trim();
  if (activation !== null && !["always", "conditional", "on-demand"].includes(activation)) {
    throw new Error(`invalid project-local skill activation for ${name}`);
  }
  const routingDeclared = ["activation", "workflowPhases", "taskTerms", "pathGlobs"]
    .some((key) => Object.hasOwn(metadata, key));
  const workflowPhases = projectLocalFrontmatterStrings(
    metadata,
    "workflowPhases",
    name,
    routingDeclared,
  );
  const taskTerms = projectLocalFrontmatterStrings(metadata, "taskTerms", name, routingDeclared);
  const pathGlobs = projectLocalFrontmatterStrings(metadata, "pathGlobs", name, routingDeclared);
  if (taskTerms.some((term) => !normalizeProjectLocalTaskText(term))) {
    throw new Error(`invalid project-local skill taskTerms for ${name}`);
  }
  for (const pattern of pathGlobs) validateProjectLocalPathGlob(pattern, name);
  if (activation === "conditional") {
    if (taskTerms.length === 0 && pathGlobs.length === 0) {
      throw new Error(`conditional project-local skill has no selectors: ${name}`);
    }
  }
  return {
    id: String(metadata.id || name),
    name,
    title: String(metadata.title || ""),
    description: String(metadata.description || useWhen || ""),
    hosts: [...PROJECT_SKILL_HOSTS],
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
    workflowPhases,
    activation,
    routingDeclared,
    taskTerms,
    pathGlobs,
    reviewAngles: arrayValue(metadata.reviewAngles),
    installGroup: String(metadata.installGroup || ""),
    excludes: arrayValue(metadata.excludes || metadata.conflicts),
    warnings,
  };
}

function projectLocalFrontmatterStrings(metadata, key, name, strict) {
  if (!Object.hasOwn(metadata, key)) return [];
  if (!Array.isArray(metadata[key])) {
    if (strict) throw new Error(`invalid project-local skill ${key} for ${name}`);
    return [];
  }
  if (metadata[key].some((entry) => typeof entry !== "string" || !entry.trim())) {
    throw new Error(`invalid project-local skill ${key} for ${name}`);
  }
  return [...new Set(metadata[key].map((entry) => entry.trim()))];
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

function preflightProjectSkillHostLinks(root, intendedLinks, previousIndex) {
  const actions = [];
  const intendedDestinations = [];
  const desiredKeys = new Set();
  const seenDestinations = new Set();

  for (const { skill, host } of intendedLinks) {
    const action = preflightProjectSkillHostDestination(root, skill, host, previousIndex);
    const destinationKey = path.resolve(action.destination);
    if (seenDestinations.has(destinationKey)) {
      throw new Error(`blocked: duplicate project skill host destination: ${action.destination}`);
    }
    seenDestinations.add(destinationKey);
    desiredKeys.add(`${host}:${skill.name}`);
    intendedDestinations.push(action.destination);
    actions.push(action);
  }

  if (!previousIndex || !Array.isArray(previousIndex.links)) {
    return { root: path.resolve(root), actions };
  }
  for (const link of previousIndex.links) {
    if (!link || !link.name || !link.host || !link.path) continue;
    const target = path.resolve(root, link.path);
    const key = `${link.host}:${link.name}`;
    if (
      desiredKeys.has(key)
      && intendedDestinations.some((destination) => path.resolve(destination) === target)
    ) {
      continue;
    }
    const stale = preflightStaleProjectSkillHostDestination(root, link, previousIndex);
    if (!stale) continue;
    const destinationKey = path.resolve(stale.destination);
    if (seenDestinations.has(destinationKey)) {
      throw new Error(`blocked: duplicate stale project skill host destination: ${stale.destination}`);
    }
    seenDestinations.add(destinationKey);
    actions.push(stale);
  }
  return { root: path.resolve(root), actions };
}

function preflightProjectSkillHostDestination(root, skill, host, previousIndex) {
  const sourceDir = path.dirname(path.resolve(root, skill.path));
  ensureChildPath(root, sourceDir);
  const sourceTreeHash = requireProjectSkillTreeHash(skill.tree_hash, skill.name);
  if (hashSkillTree(sourceDir) !== sourceTreeHash) {
    throw new Error(`blocked: project skill source tree changed during install: ${skill.name}`);
  }
  const sourceIntegrityHash = installTransactionTreeIntegrity(sourceDir);
  const hostRoot = hostSkillRoot(root, host);
  if (pathHasSymlink(root, hostRoot)) {
    throw new Error(`blocked: project skill host root may not use symlinks: ${hostRoot}`);
  }
  const destination = path.join(hostRoot, skill.name);
  ensureChildPath(hostRoot, destination);
  const relative = path.relative(root, destination);
  const snapshot = projectSkillHostDestinationSnapshot(destination);
  const base = {
    name: skill.name,
    host,
    path: relative,
    destination,
    sourceDir,
    sourceTreeHash,
    sourceIntegrityHash,
    expected: snapshot,
  };
  if (snapshot.kind === "absent") {
    return { ...base, action: "create" };
  }
  if (snapshot.kind === "symlink") {
    const desiredTarget = path.relative(path.dirname(destination), sourceDir);
    if (snapshot.target === desiredTarget) {
      return { ...base, action: "keep", status: "linked" };
    }
    const previous = previousProjectSkillSource(root, previousIndex, skill.name, host);
    if (previous && snapshot.target === previous.linkTarget) {
      return { ...base, action: "replace" };
    }
    throw new Error(
      `blocked: noncanonical or user-modified project skill host symlink: ${destination}`,
    );
  }
  if (snapshot.treeHash === sourceTreeHash && snapshot.integrityHash === sourceIntegrityHash) {
    return { ...base, action: "keep", status: "copied" };
  }
  const previous = previousProjectSkillSource(root, previousIndex, skill.name, host);
  if (
    previous
    && snapshot.treeHash === previous.treeHash
    && snapshot.integrityHash === previous.integrityHash
  ) {
    return { ...base, action: "replace" };
  }
  throw new Error(`blocked: user-modified project skill host tree differs: ${destination}`);
}

function preflightStaleProjectSkillHostDestination(root, link, previousIndex) {
  const hostRoot = legacyHostSkillRoot(root, link.path) ?? hostSkillRoot(root, link.host);
  if (pathHasSymlink(root, hostRoot)) {
    throw new Error(`blocked: stale project skill host root may not use symlinks: ${hostRoot}`);
  }
  const destination = path.resolve(root, link.path);
  ensureChildPath(hostRoot, destination);
  const expectedDestination = path.join(hostRoot, link.name);
  if (path.resolve(destination) !== path.resolve(expectedDestination)) {
    throw new Error(`blocked: invalid stale project skill host path: ${link.path}`);
  }
  const snapshot = projectSkillHostDestinationSnapshot(destination);
  if (snapshot.kind === "absent") return null;
  const previous = previousProjectSkillSource(root, previousIndex, link.name, link.host);
  if (!previous) {
    throw new Error(`blocked: stale project skill host tree has no provenance: ${destination}`);
  }
  if (snapshot.kind === "symlink") {
    if (snapshot.target !== previous.linkTarget) {
      throw new Error(`blocked: user-modified stale project skill host symlink: ${destination}`);
    }
    return {
      action: "delete",
      name: link.name,
      host: link.host,
      path: link.path,
      destination,
      expected: snapshot,
      status: "removed-stale-linked",
    };
  }
  if (snapshot.treeHash !== previous.treeHash || snapshot.integrityHash !== previous.integrityHash) {
    throw new Error(`blocked: user-modified stale project skill host tree differs: ${destination}`);
  }
  return {
    action: "delete",
    name: link.name,
    host: link.host,
    path: link.path,
    destination,
    expected: snapshot,
    status: "removed-stale-copied",
  };
}

function previousProjectSkillSource(root, previousIndex, name, host) {
  if (!previousIndex || !Array.isArray(previousIndex.skills) || !Array.isArray(previousIndex.links)) {
    return null;
  }
  const previousSkill = previousIndex.skills.find((skill) => skill?.name === name);
  const previousLink = previousIndex.links.find((link) => link?.name === name && link?.host === host);
  if (!previousSkill || !previousLink || typeof previousSkill.path !== "string") return null;
  const treeHash = requireProjectSkillTreeHash(previousSkill.tree_hash, name, false);
  const integrityHash = typeof previousLink.tree_integrity === "string"
    && /^[0-9a-f]{64}$/.test(previousLink.tree_integrity)
    ? previousLink.tree_integrity
    : null;
  if (!treeHash) return null;
  const sourceDir = path.dirname(path.resolve(root, previousSkill.path));
  ensureChildPath(root, sourceDir);
  const destination = path.resolve(root, previousLink.path);
  return {
    treeHash,
    integrityHash,
    linkTarget: path.relative(path.dirname(destination), sourceDir),
  };
}

function requireProjectSkillTreeHash(value, name, required = true) {
  if (typeof value === "string" && /^[0-9a-f]{64}$/.test(value)) return value;
  if (!required) return null;
  throw new Error(`blocked: project skill has no whole-tree hash: ${name}`);
}

function projectSkillHostDestinationSnapshot(destination) {
  const stat = lstatIfExists(destination);
  if (!stat) return { kind: "absent" };
  if (stat.isSymbolicLink()) {
    return { kind: "symlink", target: fs.readlinkSync(destination) };
  }
  if (!stat.isDirectory()) {
    throw new Error(`blocked: project skill host destination is not a directory: ${destination}`);
  }
  const skillFile = path.join(destination, "SKILL.md");
  const skillStat = lstatIfExists(skillFile);
  if (!skillStat || skillStat.isSymbolicLink() || !skillStat.isFile()) {
    throw new Error(`blocked: project skill host directory has no regular SKILL.md: ${destination}`);
  }
  return {
    kind: "directory",
    treeHash: hashSkillTree(destination),
    integrityHash: installTransactionTreeIntegrity(destination),
  };
}

function projectSkillHostSnapshotsEqual(left, right) {
  return left.kind === right.kind
    && (left.kind !== "symlink" || left.target === right.target)
    && (
      left.kind !== "directory"
      || (left.treeHash === right.treeHash && left.integrityHash === right.integrityHash)
    );
}

function applyProjectSkillHostLinkPlan(plan, persistent) {
  for (const action of plan.actions) {
    const current = projectSkillHostDestinationSnapshot(action.destination);
    if (!projectSkillHostSnapshotsEqual(current, action.expected)) {
      throw new Error(`blocked: project skill host destination changed during install: ${action.destination}`);
    }
    if (action.sourceDir && hashSkillTree(action.sourceDir) !== action.sourceTreeHash) {
      throw new Error(`blocked: project skill source tree changed during install: ${action.name}`);
    }
    if (
      action.sourceDir
      && installTransactionTreeIntegrity(action.sourceDir) !== action.sourceIntegrityHash
    ) {
      throw new Error(`blocked: project skill source modes changed during install: ${action.name}`);
    }
  }

  const applied = [];
  const results = [];
  let appliedCount = 0;
  try {
    for (const action of plan.actions) {
      if (action.action === "keep") {
        results.push(projectSkillHostLinkResult(action, action.status));
        continue;
      }
      const immediateCurrent = projectSkillHostDestinationSnapshot(action.destination);
      if (!projectSkillHostSnapshotsEqual(immediateCurrent, action.expected)) {
        throw new Error(`blocked: project skill host destination changed during install: ${action.destination}`);
      }
      if (action.sourceDir && hashSkillTree(action.sourceDir) !== action.sourceTreeHash) {
        throw new Error(`blocked: project skill source tree changed during install: ${action.name}`);
      }
      if (
        action.sourceDir
        && installTransactionTreeIntegrity(action.sourceDir) !== action.sourceIntegrityHash
      ) {
        throw new Error(`blocked: project skill source modes changed during install: ${action.name}`);
      }
      const operation = { action, backup: null, created: false };
      applied.push(operation);
      if (action.action === "replace" || action.action === "delete") {
        updateInstallTransactionHostProgress(persistent, action.destination, "backup-intent");
        operation.backup = persistent.hostBackups.get(action.destination);
        if (!operation.backup) {
          throw new Error(`blocked: project skill host transaction backup is missing: ${action.destination}`);
        }
        const beforeBackup = projectSkillHostDestinationSnapshot(action.destination);
        if (!projectSkillHostSnapshotsEqual(beforeBackup, action.expected)) {
          throw new Error(`blocked: project skill host destination changed during install: ${action.destination}`);
        }
        fs.renameSync(action.destination, operation.backup);
        updateInstallTransactionHostProgress(persistent, action.destination, "backed-up");
      }
      if (action.action === "delete") {
        updateInstallTransactionHostProgress(persistent, action.destination, "applied");
        results.push(projectSkillHostLinkResult(action, action.status));
        continue;
      }
      updateInstallTransactionHostProgress(persistent, action.destination, "apply-intent");
      const beforeCreate = projectSkillHostDestinationSnapshot(action.destination);
      if (beforeCreate.kind !== "absent") {
        throw new Error(`blocked: project skill host destination changed during install: ${action.destination}`);
      }
      const status = createProjectSkillHostExposure(plan.root, action);
      operation.created = true;
      updateInstallTransactionHostProgress(persistent, action.destination, "applied");
      results.push(projectSkillHostLinkResult(action, status));
      appliedCount += 1;
      if (appliedCount === 1) {
        const holdMs = Number.parseInt(process.env.AGENT_FLOW_TEST_HOLD_AFTER_FIRST_HOST_APPLY_MS ?? "0", 10);
        if (Number.isInteger(holdMs) && holdMs > 0 && holdMs <= 10_000) {
          fs.writeFileSync(path.join(persistent.transactionRoot, "host-apply-ready"), "ready\n", "utf8");
          Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, holdMs);
        }
        if (process.env.AGENT_FLOW_TEST_FAIL_AFTER_FIRST_HOST_APPLY === "1") {
          throw new Error("injected install failure after first host apply");
        }
      }
    }
  } catch (error) {
    throw error;
  }
  let closed = false;
  return {
    results,
    commit() {
      if (closed) return;
      closed = true;
      for (const operation of applied) {
        if (operation.backup) fs.rmSync(operation.backup, { recursive: true, force: true });
      }
    },
    rollback() {
      if (closed) return;
      closed = true;
      const rollbackErrors = rollbackProjectSkillHostOperations(applied);
      if (rollbackErrors.length > 0) {
        throw new Error(`host skill rollback failed: ${rollbackErrors.join("; ")}`);
      }
    },
  };
}

function rollbackProjectSkillHostOperations(applied) {
  const rollbackErrors = [];
  for (const operation of [...applied].reverse()) {
    try {
      if (operation.created && lstatIfExists(operation.action.destination)) {
        fs.rmSync(operation.action.destination, { recursive: true, force: true });
      }
      if (operation.backup && lstatIfExists(operation.backup)) {
        fs.renameSync(operation.backup, operation.action.destination);
      }
    } catch (rollbackError) {
      rollbackErrors.push(rollbackError instanceof Error ? rollbackError.message : String(rollbackError));
    }
  }
  return rollbackErrors;
}

function projectSkillHostLinkResult(action, status) {
  return {
    name: action.name,
    host: action.host,
    path: action.path,
    status,
    tree_integrity: action.sourceIntegrityHash ?? null,
  };
}

function projectSkillHostBackupPath(destination) {
  const parent = path.dirname(destination);
  let backup;
  do {
    backup = path.join(
      parent,
      `.${path.basename(destination)}.agent-flow-backup-${process.pid}-${crypto.randomBytes(6).toString("hex")}`,
    );
  } while (lstatIfExists(backup));
  return backup;
}

function createProjectSkillHostExposure(root, action) {
  const parent = path.dirname(action.destination);
  if (pathHasSymlink(root, parent)) {
    throw new Error(`blocked: project skill host parent may not use symlinks: ${parent}`);
  }
  fs.mkdirSync(parent, { recursive: true });
  if (hashSkillTree(action.sourceDir) !== action.sourceTreeHash) {
    throw new Error(`blocked: project skill source tree changed during install: ${action.name}`);
  }
  if (installTransactionTreeIntegrity(action.sourceDir) !== action.sourceIntegrityHash) {
    throw new Error(`blocked: project skill source modes changed during install: ${action.name}`);
  }
  const target = path.relative(parent, action.sourceDir);
  try {
    fs.symlinkSync(target, action.destination, "dir");
    return "linked";
  } catch (error) {
    if (lstatIfExists(action.destination)) throw error;
    const temporary = fs.mkdtempSync(
      path.join(parent, `.${path.basename(action.destination)}.agent-flow-copy-`),
    );
    try {
      copyTreeBinarySafe(action.sourceDir, temporary);
      if (
        hashSkillTree(action.sourceDir) !== action.sourceTreeHash
        || hashSkillTree(temporary) !== action.sourceTreeHash
        || installTransactionTreeIntegrity(action.sourceDir) !== action.sourceIntegrityHash
        || installTransactionTreeIntegrity(temporary) !== action.sourceIntegrityHash
      ) {
        throw new Error(`blocked: project skill copy tree hash mismatch: ${action.name}`);
      }
      fs.renameSync(temporary, action.destination);
      return "copied";
    } catch (copyError) {
      fs.rmSync(temporary, { recursive: true, force: true });
      throw copyError;
    }
  }
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
  const match = String(text).match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/);
  return match ? match[1] : null;
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

function hostSkillRoot(root, host) {
  if (host === "codex") {
    return path.join(root, ".agents", "skills");
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
  if (normalized.startsWith(".Codex/skills/")) {
    return path.join(root, ".Codex", "skills");
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

function readJsonIfExists(pathName) {
  if (!fs.existsSync(pathName)) {
    return null;
  }
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
  // 설치 runtime이 YAML 모듈까지 고정하므로 interpreter 자체만 확인한다.
  const kitVenvPython = path.join(KIT_ROOT, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  const leaderRoot = resolveManagedWorktreeRoot(KIT_ROOT);
  const leaderVenvPython = leaderRoot
    ? path.join(leaderRoot, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python")
    : null;
  const fixedSystemPythons = process.platform === "win32"
    ? []
    : [
        "/opt/homebrew/bin/python3.14",
        "/opt/homebrew/bin/python3.13",
        "/opt/homebrew/bin/python3.12",
        "/opt/homebrew/bin/python3.11",
        "/opt/homebrew/bin/python3.10",
        "/usr/local/bin/python3.14",
        "/usr/local/bin/python3.13",
        "/usr/local/bin/python3.12",
        "/usr/local/bin/python3.11",
        "/usr/local/bin/python3.10",
        "/usr/bin/python3",
      ].filter((candidate) => fs.existsSync(candidate));
  const candidates = [
    ...fixedSystemPythons,
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
    if (!result.error && result.status === 0) {
      return candidate;
    }
  }
  throw new Error("no Python interpreter available for status relay");
}

function assertFreshArtifact(state, phase, artifact) {
  if (!artifactIsStale(state, artifact)) {
    return;
  }
  throw new Error(`blocked: stale artifact ${artifact}`);
}

function artifactIsStale(state, artifact) {
  const enteredAt = firstValidTimestamp(
    state.phase_entered_at,
    state.updated_at,
    state.started_at,
  );
  if (enteredAt === null) {
    return false;
  }
  const artifactMtime = fs.statSync(artifact).mtimeMs;
  return artifactMtime < enteredAt;
}

function firstValidTimestamp(...values) {
  for (const value of values) {
    if (typeof value !== "string" || !value.trim()) {
      continue;
    }
    const parsed = Date.parse(value);
    if (Number.isFinite(parsed)) {
      return parsed;
    }
  }
  return null;
}

function assertCompletionMarkers(phase, artifact, root, localSkillContext = {}) {
  const content = fs.readFileSync(artifact, "utf8");
  const missing = missingMarkersForPhase(content, phase, root, localSkillContext);
  if (missing.length > 0) {
    throw new Error(`blocked: ${phase.id} artifact missing completion markers: ${missing.join(", ")}`);
  }
}

function missingMarkers(content, markers) {
  const lines = completionGateLines(content);
  return markers.filter((marker) => {
    const normalized = marker.trim().toLowerCase();
    return !markerPresent(content, lines, normalized);
  });
}

const CODE_REVIEW_LOCAL_SKILL_PHASES = new Set([
  "implement",
  "implement-fix",
  "red",
  "green",
  "refactor",
  "fix-loop",
  "final-review",
  "review",
  "pr-comment-fix",
  "pr-ci-fix",
  "multi-review",
  "architecture-review",
]);
const PROJECT_LOCAL_SKILL_APPLIED_MARKER = "project-local-skill-docs: applied";
const LOCAL_SKILL_PLAN_HASH_VERSION = 2;

function missingMarkersForPhase(content, phase, root, localSkillContext = {}) {
  const missing = missingMarkers(content, phase.required_markers ?? []);
  missing.push(...missingProjectLocalSkillMarkers(
    content,
    root,
    phase.id,
    localSkillContext.taskScope,
    localSkillContext.changedFiles,
  ));
  return missing;
}

function localSkillPromptBlock(root, phaseId, taskScope = "", changedFiles = []) {
  const docs = applicableProjectLocalSkillDocs(root, phaseId, taskScope, changedFiles);
  if (docs.length === 0) {
    return "";
  }
  const markerInstructions = CODE_REVIEW_LOCAL_SKILL_PHASES.has(phaseId)
    ? [
      "",
      "When this block appears, write these as real, unfenced Markdown lines under `## Completion Gate`:",
      "",
      "## Completion Gate",
      "project-local-skills: checked",
      `project-local-skills-used: ${docs.map((doc) => doc.name).join(", ")}`,
      PROJECT_LOCAL_SKILL_APPLIED_MARKER,
      "",
      "If this block is absent, `project-local-skills: n/a` remains valid.",
    ]
    : [
      "",
      "Apply these docs to this phase artifact. This non-code phase does not add local-skill completion markers.",
    ];
  return [
    "",
    "",
    "## Project-local skills",
    "",
    "All listed deterministically applicable project-local markdown skill docs are mandatory policy for this phase.",
    "Read every listed project-local skill before completing the phase; follow references progressively when the skill points to a separate file.",
    "",
    "Applicable docs:",
    "",
    ...docs.map((doc) => localSkillPromptLine(root, doc)),
    ...markerInstructions,
    "",
  ].join("\n");
}

function profileSkillPromptBlock(root, phaseId, workspaceRoot = process.cwd(), baseCommit = null, taskScope = null) {
  if (!CODE_REVIEW_LOCAL_SKILL_PHASES.has(phaseId)) return "";
  const index = readInstalledSkillIndex(root);
  const plan = resolveRuntimeSkillPlan(index, {
    phaseId,
    taskScope,
    changedFiles: runtimeChangedFiles(
      workspaceRoot,
      baseCommit || configuredRunBase(root, workspaceRoot).base_commit,
    ),
  });
  if (plan.skills.length === 0 && plan.missing.length === 0 && plan.missing_profiles.length === 0) return "";
  if (plan.missing_profiles.length > 0) {
    throw new Error(`missing required skill profiles in project snapshot: ${plan.missing_profiles.join(", ")}; finish the active run, reinstall from the leader checkout, and start a new run`);
  }
  if (plan.missing.length > 0) {
    throw new Error(`missing required profile skills in project snapshot: ${plan.missing.join(", ")}`);
  }
  const changed = plan.changed_files.slice(0, 20);
  return [
    "",
    "",
    "## Required profile skills",
    "",
    "Read every listed project snapshot before writing or reviewing code. Do not resolve skills from host-global directories at runtime.",
    `Active profiles: ${plan.active_profiles.join(", ") || "generic"}`,
    `Touched profiles: ${plan.touched_profiles.join(", ") || "generic"}`,
    ...(changed.length > 0 ? [`Changed files: ${changed.join(", ")}${plan.changed_files.length > changed.length ? ` (+${plan.changed_files.length - changed.length})` : ""}`] : []),
    "",
    ...plan.skills.map((skill) => (
      `- \`${skill.path}\` (\`${skill.name}\`) — \`${verifiedProfileSkillPromptPath(root, skill)}\``
    )),
    "",
  ].join("\n");
}

function verifiedProfileSkillPromptPath(root, skill) {
  const skillPath = path.resolve(root, String(skill.path || ""));
  const relative = path.relative(root, skillPath);
  if (
    path.basename(skillPath) !== "SKILL.md"
    || relative.startsWith("..")
    || path.isAbsolute(relative)
  ) {
    throw new Error(`blocked: invalid installed skill path: ${skill.name}`);
  }
  requireInstalledRegularFile(root, skillPath, `installed skill snapshot ${skill.name}`);
  if (typeof skill.tree_hash !== "string" || !skill.tree_hash) {
    throw new Error(`blocked: installed skill snapshot has no tree hash: ${skill.name}`);
  }
  if (hashSkillTree(path.dirname(skillPath)) !== skill.tree_hash) {
    throw new Error(`blocked: installed skill snapshot changed: ${skill.name}`);
  }
  return skillPath;
}

function runtimeChangedFiles(workspaceRoot, baseCommit = null) {
  const probe = safeSpawnSync("git", ["rev-parse", "--show-toplevel"], {
    cwd: workspaceRoot,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
  if (probe.error) {
    throw new Error(`blocked: changed-file query failed: ${probe.error.message}`);
  }
  if (probe.status !== 0) {
    const detail = `${probe.stderr || ""}\n${probe.stdout || ""}`;
    if (probe.status === 128 && /not a git repository/i.test(detail)) return [];
    throw new Error("blocked: changed-file query failed: git rev-parse --show-toplevel");
  }
  const root = path.resolve(probe.stdout.trim());
  if (!probe.stdout.trim()) {
    throw new Error("blocked: changed-file query failed: git rev-parse returned no repository root");
  }
  if (!root) return [];
  const files = new Set();
  const collect = (args) => {
    const result = safeSpawnSync("git", args, {
      cwd: root,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    });
    if (result.error || result.status !== 0) {
      throw new Error(`blocked: changed-file query failed: git ${args.join(" ")}`);
    }
    for (const line of result.stdout.split(/\r?\n/).map((value) => value.trim()).filter(Boolean)) {
      files.add(line.replaceAll("\\", "/"));
    }
  };
  collect(["diff", "--name-only", "--diff-filter=ACMRD", "HEAD"]);
  collect(["diff", "--name-only", "--diff-filter=ACMRD", "--cached"]);
  collect(["ls-files", "--others", "--exclude-standard"]);
  if (baseCommit) collect(["diff", "--name-only", "--diff-filter=ACMRD", `${baseCommit}...HEAD`]);
  return [...files].sort();
}

function localSkillContextForState(state, root) {
  const workspaceRoot = workspaceRootForState(state, root);
  const baseCommit = state.base_commit || configuredRunBase(root, workspaceRoot).base_commit;
  return {
    taskScope: typeof state.task === "string" ? state.task : "",
    changedFiles: runtimeChangedFiles(workspaceRoot, baseCommit),
  };
}

function configuredRunBase(configRoot, workspaceRoot) {
  const kitPath = path.join(configRoot, ".agent-flow", "kit.json");
  const kit = lstatIfExists(kitPath) ? readInstalledKit(configRoot) : {};
  const profile = configuredPrimaryProfile(kit);
  const profilePath = path.join(configRoot, ".agent-flow", "profiles", `${profile}.yaml`);
  let baseRef = "HEAD";
  if (fs.existsSync(profilePath)) {
    const payload = readInstalledProfilePayload(configRoot, profile);
    const branching = payload.branching;
    if (
      branching
      && typeof branching === "object"
      && !Array.isArray(branching)
      && typeof branching.base === "string"
    ) {
      baseRef = branching.base;
    }
  } else if (hasConfiguredProfile(kit)) {
    throw new Error(`blocked: unknown installed profile: ${profile}`);
  }
  const gitRoot = resolveGitTopLevel(workspaceRoot);
  if (!gitRoot) return { base_ref: baseRef, base_commit: null };
  const baseCommit = baseRef === "HEAD"
    ? gitOutput(gitRoot, ["rev-parse", "HEAD"])
    : gitOutput(gitRoot, ["merge-base", "HEAD", baseRef])
      || gitOutput(gitRoot, ["rev-parse", `${baseRef}^{commit}`])
      || gitOutput(gitRoot, ["rev-parse", "HEAD"]);
  return { base_ref: baseRef, base_commit: baseCommit };
}

function localSkillPromptLine(root, doc) {
  const absolutePath = path.isAbsolute(doc.path)
    ? doc.path
    : path.join(root, doc.path);
  return `- \`${doc.path}\` (\`${doc.name}\`) — \`${absolutePath}\``;
}

function missingProjectLocalSkillMarkers(content, root, phaseId, taskScope = "", changedFiles = []) {
  if (!CODE_REVIEW_LOCAL_SKILL_PHASES.has(phaseId)) {
    return [];
  }
  const docs = applicableProjectLocalSkillDocs(root, phaseId, taskScope, changedFiles);
  if (docs.length === 0) {
    return [];
  }
  const values = completionGateMarkerValues(content);
  const missing = [];
  if (values.get("project-local-skills") !== "checked") {
    missing.push("project-local-skills: checked");
  }
  const used = (values.get("project-local-skills-used") ?? "").trim();
  const usedNames = new Set(
    used
      .split(",")
      .map((name) => name.trim().replace(/^`|`$/g, "").toLowerCase())
      .filter(Boolean),
  );
  if (["", "n/a", "none", "optional"].includes(used)) {
    missing.push("project-local-skills-used: <applicable local skill list>");
  } else if (!docs.every((doc) => usedNames.has(doc.name.toLowerCase()))) {
    missing.push("project-local-skills-used: <applicable local skill list>");
  }
  if (values.get("project-local-skill-docs") !== "applied") {
    missing.push(PROJECT_LOCAL_SKILL_APPLIED_MARKER);
  }
  return missing;
}

function applicableProjectLocalSkillDocs(root, phaseId, taskScope = "", changedFiles = []) {
  return projectLocalSkillDocs(root)
    .filter((doc) => projectLocalSkillApplies(doc, phaseId, taskScope, changedFiles))
    .sort((a, b) => compareCodePoints(a.name, b.name));
}

function projectLocalSkillDocs(root) {
  const indexDocs = localSkillDocsFromIndex(root);
  return dedupeLocalSkillDocs([...indexDocs, ...localSkillDocsFromTree(root)]);
}

function projectLocalSkillPlanHash(root) {
  const payload = {
    version: LOCAL_SKILL_PLAN_HASH_VERSION,
    skills: projectLocalSkillDocs(root)
      .sort((left, right) => compareCodePoints(left.name, right.name))
      .map((doc) => [
        doc.name,
        doc.path.replaceAll("\\", "/"),
        doc.source,
        doc.tree_hash,
        doc.activation,
        [...doc.workflow_phases].sort(compareCodePoints),
        [...doc.task_terms].sort(compareCodePoints),
        [...doc.path_globs].sort(compareCodePoints),
      ]),
  };
  return crypto.createHash("sha256").update(JSON.stringify(payload)).digest("hex");
}

function localSkillDocsFromIndex(root) {
  const indexPath = path.join(root, ".agent-flow", "skills", "index.json");
  const indexStat = lstatIfExists(indexPath);
  if (!indexStat) {
    return [];
  }
  requireInstalledRegularFile(root, indexPath, "project-local skill index");
  let payload;
  try {
    payload = JSON.parse(fs.readFileSync(indexPath, "utf8"));
  } catch (error) {
    throw new Error(`blocked: unreadable project-local skill index ${indexPath}: ${error.message}`);
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload) || !Array.isArray(payload.skills)) {
    throw new Error(`blocked: invalid project-local skill index: ${indexPath}`);
  }
  const externalExposureSkillNames = new Set(
    normalizedExternalExposureSkillNames(payload.selection),
  );
  return payload.skills.flatMap((skill) => {
    if (!skill || typeof skill !== "object" || Array.isArray(skill)) {
      throw new Error(`blocked: invalid project-local skill index: ${indexPath}`);
    }
    const discoverySource = skill.discovery_source ?? skill.source;
    const provenanceSource = skill.source;
    const exposedExternal = isPortableSkillName(skill.name)
      && externalExposureSkillNames.has(portableSkillCasefold(skill.name))
      && ["host-bootstrap", "shared"].includes(provenanceSource);
    if (!["local", "project"].includes(discoverySource) && !exposedExternal) return [];
    const relPath = String(skill.path ?? "");
    if (!isProjectLocalSkillPath(relPath)) {
      throw new Error(`blocked: invalid project-local skill path in index: ${JSON.stringify(relPath)}`);
    }
    const rawName = Object.hasOwn(skill, "name")
      ? skill.name
      : path.basename(path.dirname(relPath));
    if (!isPortableSkillName(rawName)) {
      throw new Error(`blocked: invalid project-local skill name in index: ${JSON.stringify(rawName)}`);
    }
    const name = portableSkillCasefold(rawName);
    const activation = projectLocalSkillActivation(skill, name);
    const workflowPhases = projectLocalRoutingStrings(skill, "workflowPhases", name);
    const taskTerms = projectLocalRoutingStrings(skill, "taskTerms", name);
    const pathGlobs = projectLocalRoutingStrings(skill, "pathGlobs", name);
    if (taskTerms.some((term) => !normalizeProjectLocalTaskText(term))) {
      throw new Error(`blocked: invalid project-local skill taskTerms for ${name}`);
    }
    for (const pattern of pathGlobs) validateProjectLocalPathGlob(pattern, name);
    if (activation === "conditional" && taskTerms.length === 0 && pathGlobs.length === 0) {
      throw new Error(`blocked: conditional project-local skill has no selectors: ${name}`);
    }
    const validated = validateIndexedProjectLocalSkill(root, relPath, skill.tree_hash);
    return [{
      name,
      path: relPath.replaceAll("\\", "/"),
      source: exposedExternal ? provenanceSource : discoverySource,
      tree_hash: validated.treeHash,
      description: [skill.description, skill.trigger].filter(Boolean).join(" "),
      activation,
      workflow_phases: workflowPhases,
      task_terms: taskTerms,
      path_globs: pathGlobs,
    }];
  });
}

function localSkillDocsFromTree(root) {
  const localBase = path.join(root, ".agent-flow", "local-skills");
  const projectBase = path.join(root, "skills");
  const discovered = [];
  if (lstatIfExists(localBase)) {
    requireProjectLocalDirectory(root, localBase, "project-local skill root");
    discovered.push(...discoverSkills(localBase, "local", root));
  }
  if (!samePath(root, KIT_ROOT) && lstatIfExists(projectBase)) {
    requireProjectLocalDirectory(root, projectBase, "project-local skill root");
    discovered.push(...discoverSkills(projectBase, "project", root));
  }
  return discovered.map((skill) => {
    const name = portableSkillCasefold(skill.name);
    const routing = {
      activation: skill.activation,
      workflowPhases: skill.workflowPhases,
      taskTerms: skill.taskTerms ?? [],
      pathGlobs: skill.pathGlobs ?? [],
    };
    const activation = projectLocalSkillActivation(routing, name);
    const workflowPhases = projectLocalRoutingStrings(routing, "workflowPhases", name);
    const taskTerms = projectLocalRoutingStrings(routing, "taskTerms", name);
    const pathGlobs = projectLocalRoutingStrings(routing, "pathGlobs", name);
    if (taskTerms.some((term) => !normalizeProjectLocalTaskText(term))) {
      throw new Error(`blocked: invalid project-local skill taskTerms for ${name}`);
    }
    for (const pattern of pathGlobs) validateProjectLocalPathGlob(pattern, name);
    if (activation === "conditional" && taskTerms.length === 0 && pathGlobs.length === 0) {
      throw new Error(`blocked: conditional project-local skill has no selectors: ${name}`);
    }
    return {
      name,
      path: skill.path.split(path.sep).join("/"),
      source: skill.source,
      tree_hash: skill.tree_hash,
      description: localSkillMetadataText(path.resolve(root, skill.path)),
      activation,
      workflow_phases: workflowPhases,
      task_terms: taskTerms,
      path_globs: pathGlobs,
    };
  });
}

function localSkillMetadataText(skillPath) {
  try {
    const text = fs.readFileSync(skillPath, "utf8");
    const frontmatter = text.match(/^---\n([\s\S]*?)\n---/);
    return frontmatter ? frontmatter[1] : text.split(/\r?\n/).slice(0, 20).join("\n");
  } catch (_error) {
    return "";
  }
}

function isProjectLocalSkillPath(relPath) {
  const normalized = relPath.replaceAll("\\", "/");
  if (normalized.startsWith("/") || normalized.includes("//")) return false;
  const parts = normalized.split("/");
  return (
    parts.length === 4
    && [[".agent-flow", "local-skills"], [".agent-flow", "skills"]]
      .some(([first, second]) => parts[0] === first && parts[1] === second)
    && ![".", ".."].includes(parts[2])
    && parts[3] === "SKILL.md"
  ) || (
    parts.length === 3
    && parts[0] === "skills"
    && ![".", ".."].includes(parts[1])
    && parts[2] === "SKILL.md"
  );
}

function validateIndexedProjectLocalSkill(root, relPath, expectedTreeHash) {
  const normalized = relPath.replaceAll("\\", "/");
  const parts = normalized.split("/");
  const base = parts[0] === "skills"
    ? path.join(root, "skills")
    : path.join(root, parts[0], parts[1]);
  requireProjectLocalDirectory(root, base, "project-local skill root");
  const skillPath = path.resolve(root, normalized);
  ensureChildPath(base, skillPath);
  const skillDir = path.dirname(skillPath);
  requireProjectLocalDirectory(root, skillDir, "project-local skill");
  const fileStat = lstatIfExists(skillPath);
  if (!fileStat || fileStat.isSymbolicLink() || !fileStat.isFile()) {
    throw new Error(`blocked: project-local SKILL.md must be a regular file: ${skillPath}`);
  }
  const treeHash = hashSkillTree(skillDir);
  if (expectedTreeHash !== undefined && expectedTreeHash !== null) {
    if (typeof expectedTreeHash !== "string" || !expectedTreeHash) {
      throw new Error(`blocked: invalid project-local skill tree hash in index: ${JSON.stringify(relPath)}`);
    }
    if (treeHash !== expectedTreeHash) {
      throw new Error(`blocked: project-local skill tree hash mismatch: ${JSON.stringify(relPath)}`);
    }
  }
  return { skillPath, treeHash };
}

function requireProjectLocalDirectory(root, pathName, label) {
  const lexicalRoot = path.resolve(root);
  const lexicalPath = path.resolve(pathName);
  const relative = path.relative(lexicalRoot, lexicalPath);
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(`blocked: ${label} escapes the project: ${pathName}`);
  }
  let cursor = lexicalRoot;
  for (const part of relative.split(path.sep).filter(Boolean)) {
    cursor = path.join(cursor, part);
    const stat = lstatIfExists(cursor);
    if (!stat || stat.isSymbolicLink() || !stat.isDirectory()) {
      throw new Error(`blocked: ${label} must be a real directory: ${cursor}`);
    }
  }
  let realRoot;
  let realPath;
  try {
    realRoot = fs.realpathSync(lexicalRoot);
    realPath = fs.realpathSync(lexicalPath);
  } catch (error) {
    throw new Error(`blocked: ${label} is unreadable: ${pathName}: ${error.message}`);
  }
  ensureChildPath(realRoot, realPath);
}

function projectLocalSkillActivation(skill, name) {
  if (skill.activation === undefined || skill.activation === null) return "on-demand";
  if (!["always", "conditional", "on-demand"].includes(skill.activation)) {
    throw new Error(`blocked: invalid project-local skill activation for ${name}`);
  }
  return skill.activation;
}

function projectLocalRoutingStrings(skill, key, name) {
  if (!Object.hasOwn(skill, key)) return [];
  if (!Array.isArray(skill[key]) || skill[key].some((entry) => typeof entry !== "string" || !entry.trim())) {
    throw new Error(`blocked: invalid project-local skill ${key} for ${name}`);
  }
  return [...new Set(skill[key].map((entry) => entry.trim()))];
}

function projectLocalSkillApplies(doc, phaseId, taskScope, changedFiles) {
  if (doc.activation === "always") {
    if (doc.workflow_phases.length > 0) {
      return doc.workflow_phases.some(
        (phase) => portableProjectLocalCasefold(phase) === portableProjectLocalCasefold(phaseId),
      );
    }
    return CODE_REVIEW_LOCAL_SKILL_PHASES.has(phaseId);
  }
  if (doc.activation === "on-demand") return false;
  if (
    doc.workflow_phases.length > 0
    && !doc.workflow_phases.some((phase) => portableProjectLocalCasefold(phase) === portableProjectLocalCasefold(phaseId))
  ) {
    return false;
  }
  const normalizedTask = ` ${normalizeProjectLocalTaskText(taskScope)} `;
  const taskMatch = doc.task_terms.some(
    (term) => normalizedTask.includes(` ${normalizeProjectLocalTaskText(term)} `),
  );
  const normalizedFiles = changedFiles.map(normalizeProjectLocalRepoPath);
  const pathMatch = doc.path_globs.some(
    (pattern) => normalizedFiles.some((file) => projectLocalPathGlobMatches(pattern, file)),
  );
  return taskMatch || pathMatch;
}

function portableProjectLocalCasefold(value) {
  return String(value).normalize("NFKC").toLowerCase().replaceAll("ß", "ss").replaceAll("ς", "σ");
}

function normalizeProjectLocalTaskText(value) {
  return portableProjectLocalCasefold(value).replace(/[^\p{L}\p{N}]+/gu, " ").trim().replace(/\s+/g, " ");
}

function normalizeProjectLocalRepoPath(value) {
  return portableProjectLocalCasefold(value).replaceAll("\\", "/");
}

function validateProjectLocalPathGlob(pattern, name) {
  const normalized = String(pattern).replaceAll("\\", "/");
  const parts = normalized.split("/");
  if (
    !normalized
    || normalized.startsWith("/")
    || /^[A-Za-z]:\//.test(normalized)
    || normalized.includes("\0")
    || parts.some((part) => ["", ".", ".."].includes(part))
  ) {
    throw new Error(`blocked: invalid project-local skill pathGlobs for ${name}`);
  }
}

function projectLocalPathGlobMatches(pattern, candidate) {
  const folded = normalizeProjectLocalRepoPath(pattern);
  let expression = "^";
  for (let index = 0; index < folded.length;) {
    if (folded.startsWith("**/", index)) {
      expression += "(?:.*/)?";
      index += 3;
    } else if (folded.startsWith("**", index)) {
      expression += ".*";
      index += 2;
    } else if (folded[index] === "*") {
      expression += "[^/]*";
      index += 1;
    } else if (folded[index] === "?") {
      expression += "[^/]";
      index += 1;
    } else {
      expression += folded[index].replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      index += 1;
    }
  }
  return new RegExp(`${expression}$`, "u").test(candidate);
}

function dedupeLocalSkillDocs(docs) {
  const byName = new Map();
  const priority = { local: 0, project: 1 };
  for (const doc of docs) {
    const key = portableSkillCasefold(doc.name);
    const existing = byName.get(key);
    if (!existing) {
      byName.set(key, doc);
      continue;
    }
    if (
      existing.path.replaceAll("\\", "/") === doc.path.replaceAll("\\", "/")
      && existing.tree_hash
      && doc.tree_hash
      && existing.tree_hash !== doc.tree_hash
    ) {
      throw new Error(`blocked: conflicting project-local skill snapshot: ${doc.name}`);
    }
    if (existing.path.replaceAll("\\", "/") === doc.path.replaceAll("\\", "/")) {
      continue;
    }
    const existingPriority = priority[existing.source] ?? 99;
    const nextPriority = priority[doc.source] ?? 99;
    if (nextPriority < existingPriority) {
      byName.set(key, doc);
    } else if (nextPriority === existingPriority) {
      throw new Error(`blocked: conflicting project-local skill paths: ${doc.name}`);
    }
  }
  return [...byName.values()];
}

function markerPresent(content, gateLines, marker) {
  if (marker.startsWith("#")) {
    return headingPresent(content, marker);
  }
  return gateLines.some((line) => lineMatchesMarker(line, marker));
}

function headingPresent(content, marker) {
  let inFence = false;
  for (const line of content.split(/\r?\n/)) {
    if (line.startsWith("    ") || line.startsWith("\t")) {
      continue;
    }
    const stripped = line.trim();
    const lowered = stripped.toLowerCase();
    if (lowered.startsWith("```") || lowered.startsWith("~~~")) {
      inFence = !inFence;
      continue;
    }
    if (inFence) {
      continue;
    }
    if (lowered.startsWith("#") && lowered === marker) {
      return true;
    }
  }
  return false;
}

function completionGateLines(content) {
  const lines = content.split(/\r?\n/);
  const out = [];
  let inGate = false;
  let inFence = false;
  for (const line of lines) {
    if (line.startsWith("    ") || line.startsWith("\t")) {
      continue;
    }
    const stripped = line.trim();
    const lowered = stripped.toLowerCase();
    if (lowered.startsWith("```") || lowered.startsWith("~~~")) {
      inFence = !inFence;
      continue;
    }
    if (inFence) {
      continue;
    }
    if (lowered.startsWith("#")) {
      const heading = lowered.replace(/^#+/, "").trim();
      if (heading === "completion gate") {
        inGate = true;
        continue;
      }
      if (inGate) {
        break;
      }
    }
    if (inGate) {
      out.push(normalizeCompletionMarkerLine(stripped).toLowerCase());
    }
  }
  return out;
}

function completionGateMarkerValues(content) {
  const values = new Map();
  for (const line of completionGateLines(content)) {
    const separator = line.indexOf(":");
    if (separator !== -1) {
      values.set(line.slice(0, separator).trim(), line.slice(separator + 1).trim());
    }
  }
  return values;
}

function normalizeCompletionMarkerLine(line) {
  let candidate = line.trim();
  if (candidate.startsWith("+")) {
    candidate = candidate.slice(1).trim();
  }
  const lowered = candidate.toLowerCase();
  for (const prefix of ["- [x] ", "- [ ] ", "- ", "* "]) {
    if (lowered.startsWith(prefix)) {
      return candidate.slice(prefix.length).trim();
    }
  }
  return candidate;
}

function lineMatchesMarker(line, marker) {
  if (marker.endsWith(":")) {
    return line.startsWith(marker) && line.slice(marker.length).trim().length > 0;
  }
  const separator = marker.indexOf(":");
  if (separator !== -1 && marker.slice(separator + 1).includes("|")) {
    const lineSeparator = line.indexOf(":");
    if (lineSeparator === -1) {
      return false;
    }
    const lineKey = line.slice(0, lineSeparator).trim();
    const markerKey = marker.slice(0, separator).trim();
    const allowed = marker
      .slice(separator + 1)
      .split("|")
      .map((value) => value.trim())
      .filter(Boolean);
    // n/a 마커는 artifact에서 optional로 써도 같은 비적용 상태로 인정한다.
    if (allowed.includes("n/a")) {
      allowed.push("optional");
    }
    return lineKey === markerKey && allowed.includes(line.slice(lineSeparator + 1).trim());
  }
  return line === marker;
}

function artifactHasFailureMarkers(pathName) {
  const content = fs.readFileSync(pathName, "utf8");
  return completionGateLines(content).some((line) => {
    const separator = line.indexOf(":");
    if (separator === -1) {
      return false;
    }
    const key = line.slice(0, separator).trim();
    const value = line.slice(separator + 1).trim();
    if (value === "fail") {
      return true;
    }
    return key === "missing-required-profile-skills" && !["", "none", "n/a"].includes(value);
  });
}

const FIX_LOOP_MAX_ROUNDS = 3;

function nextPhaseIndex(state, phases, phase, artifact, resolvedRouteKey = null) {
  if (!phase.routes) {
    if (phase.multi_review || phase.id === "gates") {
      throw new Error(`blocked: ${phase.id} requires explicit routes`);
    }
    return state.phase_index + 1;
  }
  const key = resolvedRouteKey ?? nodeRouteKey(phase, artifact);
  if (key === "invalid-route") {
    throw new Error(`blocked: ${phase.id} artifact has contradictory or multiple route fields (invalid-route)`);
  }
  const target = phase.routes[key] ?? phase.routes.default;
  if (!target) {
    throw new Error(`blocked: ${phase.id} artifact has no route for ${key}`);
  }
  if (target === "block") {
    if (phase.id === "pr-watch" && key === "pending") {
      throw new Error("blocked: PR watch is pending");
    }
    throw new Error(`blocked: ${phase.id} route ${key}`);
  }
  // gates뿐 아니라 fix-loop로 라우팅하는 모든 phase에 같은 상한을 적용한다 (Python runner와 동일).
  if (target === "fix-loop") {
    const rounds = (state.fix_loop_rounds ?? 0) + 1;
    if (rounds > FIX_LOOP_MAX_ROUNDS) {
      throw new Error(`blocked: fix-loop exceeded ${FIX_LOOP_MAX_ROUNDS} rounds — escalate to user`);
    }
  }
  return phaseIndex(phases, target);
}

function syncRouteArtifacts(runDir, phases, currentIndex, nextIndex) {
  if (nextIndex <= currentIndex) {
    for (const phase of phases.slice(nextIndex, currentIndex + 1)) {
      const artifact = path.join(runDir, phase.artifact);
      if (fs.existsSync(artifact)) {
        fs.unlinkSync(artifact);
      }
    }
    return;
  }
  if (nextIndex <= currentIndex + 1) {
    return;
  }
  for (const phase of phases.slice(currentIndex + 1, nextIndex)) {
    const artifact = path.join(runDir, phase.artifact);
    if (fs.existsSync(artifact)) {
      continue;
    }
    fs.mkdirSync(path.dirname(artifact), { recursive: true });
    fs.writeFileSync(
      artifact,
      `# ${phase.id}\n\nstatus: skipped\nreason: route_to_${phases[nextIndex].id}\n`,
      "utf8",
    );
  }
}

function nextFixLoopRounds(state, phase, nextPhase) {
  const routesToFixLoop = Boolean(phase.routes) && Object.values(phase.routes).includes("fix-loop");
  if (routesToFixLoop && nextPhase?.id === "fix-loop") {
    return (state.fix_loop_rounds ?? 0) + 1;
  }
  if (phase.id === "gates" && routesToFixLoop && state.fix_loop_rounds !== undefined) {
    return undefined;
  }
  return state.fix_loop_rounds;
}

function nodeRouteKey(phase, artifact) {
  if (phase.id === "gates") {
    return readGatesRouteKey(artifact);
  }
  if (phase.multi_review) {
    const verdict = readMultiReviewVerdict(artifact, phase.id);
    if (verdict === "approve" || verdict === "request-changes" || verdict === "blocked") {
      if (verdict === "approve" && phase.routes?.["request-changes"] && artifactHasFailureMarkers(artifact)) {
        return "request-changes";
      }
      return verdict;
    }
    throw new Error("blocked: multi-review artifact must include verdict: approve or verdict: request-changes");
  }
  if (phase.id === "pr-watch") {
    const status = readPrWatchRouteKey(artifact);
    if (status === "invalid-route") {
      return status;
    }
    if (["green", "merged", "skipped", "comments", "has_comments", "ci-failed", "ci_failed", "pending", "closed", "error"].includes(status)) {
      return status;
    }
    return "default";
  }
  if (phase.id === "plan-review" || phase.id === "architecture-review" || phase.id === "merge-approval") {
    const key = readSingleReviewRouteKey(artifact, phase.id);
    if (key === "invalid-route") {
      return key;
    }
    if (["approve", "request-changes", "blocked"].includes(key)) {
      if (key === "approve" && phase.routes?.["request-changes"] && artifactHasFailureMarkers(artifact)) {
        return "request-changes";
      }
      return key;
    }
    return "default";
  }
  return readGenericRouteKey(artifact);
}

function phaseIndex(phases, id) {
  const index = phases.findIndex((phase) => phase.id === id);
  if (index === -1) {
    throw new Error(`unknown phase: ${id}`);
  }
  return index;
}

function readGenericRouteKey(pathName) {
  const content = fs.readFileSync(pathName, "utf8");
  const fields = [...content.matchAll(/^(status|verdict):\s*([a-z_-]+)\s*$/gim)];
  if (fields.length > 1) {
    return "invalid-route";
  }
  if (fields.length === 0) {
    return "default";
  }
  const key = fields[0][2].trim().toLowerCase();
  return new Set([
    "blocked",
    "request-changes",
    "ci-failed",
    "ci_failed",
    "comments",
    "has_comments",
    "skipped",
    "pending",
    "green",
    "approve",
    "merged",
    "closed",
    "error",
  ]).has(key) ? key : "default";
}

function readPrWatchRouteKey(pathName) {
  const content = fs.readFileSync(pathName, "utf8");
  const fields = [...content.matchAll(/^(status|verdict):\s*([a-z_-]+)\s*$/gim)];
  if (fields.length > 1) {
    return "invalid-route";
  }
  if (fields.length === 0) {
    return "default";
  }
  if (fields[0][1].trim().toLowerCase() !== "status") {
    return "invalid-route";
  }
  return fields[0][2].trim().toLowerCase();
}

function readSingleReviewRouteKey(pathName, phaseId) {
  const content = fs.readFileSync(pathName, "utf8");
  const fields = [...content.matchAll(/^(status|verdict):\s*([^\r\n]*)$/gim)];
  if (fields.length > 1) {
    return "invalid-route";
  }
  if (fields.length === 0) {
    return "default";
  }
  const field = fields[0][1].trim().toLowerCase();
  const value = fields[0][2].trim().toLowerCase();
  if (["plan-review", "merge-approval"].includes(phaseId) && field !== "verdict") {
    return "invalid-route";
  }
  return ["approve", "request-changes", "blocked"].includes(value) ? value : "default";
}

function assertMinReviewerCount(pathName, minimum) {
  const content = fs.readFileSync(pathName, "utf8");
  const reviewers = parseReviewerVerdicts(content);
  if (reviewers.size >= minimum) {
    return;
  }
  throw new Error(`blocked: multi-review artifact must contain at least ${minimum} independent reviewer verdicts`);
}

function readMultiReviewVerdict(pathName, phaseId = "") {
  const content = fs.readFileSync(pathName, "utf8");
  const overall = readMultiReviewOverallVerdict(content, phaseId);
  if (overall === "blocked" && phaseId === "architecture-review") {
    return "blocked";
  }
  if (overall && !["approve", "request-changes"].includes(overall)) {
    throw new Error("blocked: multi-review artifact overall verdict must be approve or request-changes");
  }
  const reviewers = parseReviewerVerdicts(content);
  if (reviewers.size < 1) {
    throw new Error("blocked: multi-review artifact must contain at least 1 independent sub-agent reviewer verdict");
  }
  const verdicts = [...reviewers.values()];
  if (overall === "request-changes" || verdicts.includes("request-changes")) {
    return "request-changes";
  }
  if (reviewers.size < 2) {
    throw new Error("blocked: multi-review artifact must contain at least 2 independent sub-agent reviewer verdicts");
  }
  if (overall === "approve" && verdicts.every((verdict) => verdict === "approve")) {
    return "approve";
  }
  throw new Error("blocked: multi-review artifact must include matching reviewer verdicts and overall verdict");
}

function readMultiReviewOverallVerdict(content, phaseId = "") {
  // Python runner와 같은 heading alias(Overall/Final [Verdict])를 인정한다.
  const sections = content.split(/^##[ \t]+(?:Overall|Final)(?:[ \t]+Verdict)?[ \t]*$/im);
  if (sections.length < 2) {
    if (phaseId === "architecture-review") {
      const blockedFields = [...content.matchAll(/^(verdict|status):\s*(blocked)\s*$/gim)];
      if (blockedFields.length === 1) {
        return "blocked";
      }
      if (blockedFields.length > 1) {
        return "invalid-verdict";
      }
    }
    return undefined;
  }
  if (sections.length > 2) {
    return "invalid-verdict";
  }
  const overallBlock = sections[sections.length - 1].split(/^#{1,6}[ \t]+/m, 1)[0] ?? "";
  const routeFields = [...overallBlock.matchAll(/^(verdict|status):\s*([a-z_-]+)\s*$/gim)];
  if (routeFields.length === 0) {
    return undefined;
  }
  if (routeFields.length !== 1) {
    return "invalid-verdict";
  }
  const field = routeFields[0][1].toLowerCase();
  const value = routeFields[0][2].toLowerCase();
  if (phaseId === "architecture-review") {
    if (field === "status") {
      return value === "blocked" ? "blocked" : "invalid-verdict";
    }
    return ["approve", "request-changes", "blocked"].includes(value)
      ? value
      : "invalid-verdict";
  }
  return field === "verdict" && ["approve", "request-changes"].includes(value)
    ? value
    : "invalid-verdict";
}

function parseReviewerVerdicts(content) {
  // reviewer id를 키로 정규화해 한 reviewer가 여러 번 approve를 찍어도 독립 리뷰로 세지 않는다.
  const reviewers = new Map();
  const stateFor = (reviewerId) => {
    if (!reviewers.has(reviewerId)) {
      reviewers.set(reviewerId, { subagent: false, verdict: undefined });
    }
    return reviewers.get(reviewerId);
  };
  const setVerdict = (reviewerId, verdict) => {
    const state = stateFor(reviewerId);
    if (state.verdict !== undefined) {
      throw new Error(`blocked: multi-review reviewer ${reviewerId} has multiple verdict lines`);
    }
    state.verdict = verdict;
  };
  const sourcePattern = /^(reviewer[-_ ]?[a-z0-9-]+)\s+reviewer[-_ ]?source:\s*(.+)$/gim;
  for (const match of content.matchAll(sourcePattern)) {
    const reviewerId = normalizeReviewerId(match[1]);
    if (reviewerId && isSubagentSource(match[2])) {
      stateFor(reviewerId).subagent = true;
    }
  }
  const linePattern = /^reviewer[-_ ]?([a-z0-9-]*)[^\n]*verdict:\s*(approve|request-changes)\s*$/gim;
  for (const match of content.matchAll(linePattern)) {
    const reviewerId = normalizeReviewerId(match[1]);
    if (!reviewerId) {
      continue;
    }
    if (!["approve", "request-changes"].includes(match[2])) {
      continue;
    }
    setVerdict(reviewerId, match[2]);
  }
  const sections = content.split(/^##[ \t]+Reviewer[ \t]*([^\n]*)/im);
  for (let index = 1; index < sections.length; index += 2) {
    const reviewerId = normalizeReviewerHeadingId(sections[index]);
    if (!reviewerId) {
      continue;
    }
    const reviewerBlock = sections[index + 1]?.split(/\n##[ \t]+(?:Reviewer|Overall|Final)\b/i, 1)[0] ?? "";
    if (hasSubagentSource(reviewerBlock)) {
      stateFor(reviewerId).subagent = true;
    }
    const verdicts = [...reviewerBlock.matchAll(/^\s*verdict:\s*(approve|request-changes)\s*$/gim)]
      .map((match) => match[1]);
    if (verdicts.length > 1) {
      throw new Error(`blocked: multi-review reviewer ${reviewerId} has multiple verdict lines`);
    }
    if (verdicts.length === 1) {
      setVerdict(reviewerId, verdicts[0]);
    }
  }
  return new Map(
    [...reviewers.entries()]
      .filter(([, state]) => state.subagent && state.verdict)
      .map(([reviewerId, state]) => [reviewerId, state.verdict])
  );
}

function hasSubagentSource(value) {
  const sourcePattern = /(?:^|\n)\s*reviewer[-_ ]?source\s*:\s*([^\n]+)/gi;
  return [...String(value).matchAll(sourcePattern)].some((match) => isSubagentSource(match[1]));
}

function isSubagentSource(value) {
  const normalized = String(value).toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  return [
    "sub agent",
    "subagent",
    "host sub agent",
    "host subagent",
    "active host sub agent",
    "active host subagent",
  ].includes(normalized);
}

function normalizeReviewerId(value) {
  // 섹션 라벨과 종합 verdict는 독립 reviewer id로 세지 않는다.
  const genericLabels = new Set([
    "verdict",
    "verdicts",
    "overall",
    "final",
    "summary",
    "review",
    "reviews",
    "feedback",
    "report",
    "reports",
    "assessment",
    "assessments",
    "analysis",
    "analyses",
    "decision",
    "decisions",
    "conclusion",
    "conclusions",
    "status",
    "statuses",
    "approval",
    "approvals",
    "note",
    "notes",
    "finding",
    "findings",
    "comment",
    "comments",
    "output",
    "outputs",
    "result",
    "results",
    "scope",
    "check",
    "checks",
    "checklist",
    "details",
    "detail",
  ]);
  const key = String(value ?? "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .replace(/^reviewer\b/, "")
    .trim();
  if (!key || key.split(/\s+/).some((part) => genericLabels.has(part))) {
    return "";
  }
  return key;
}

function normalizeReviewerHeadingId(value) {
  // Reviewer heading은 1-2 단어 id(claude, agent 1 등)만 독립 id로 인정한다.
  // 긴 서술형 heading은 reviewer가 아니라 prose일 가능성이 높아 제외한다.
  const key = normalizeReviewerId(value);
  return /^[a-z0-9]+(?: [a-z0-9]+)?$/.test(key) ? key : "";
}

function readGatesPassed(pathName) {
  return readGatesRouteKey(pathName) === "green";
}

function readGatesRouteKey(pathName) {
  try {
    const content = fs.readFileSync(pathName, "utf8");
    const data = JSON.parse(content);
    if (hasDuplicateJsonObjectKeys(content)) {
      return "invalid-route";
    }
    if (!data || typeof data !== "object" || Array.isArray(data) || typeof data.passed !== "boolean") {
      return "default";
    }
    if (Object.hasOwn(data, "status") && typeof data.status !== "string") {
      return "invalid-route";
    }
    if (typeof data.status === "string") {
      const status = data.status.trim().toLowerCase().replace(/_/g, "-");
      if (data.passed === true && ["green", "approve"].includes(status)) {
        return gateResultsProvePass(data.results) ? status : "default";
      }
      if (data.passed === false && ["request-changes", "blocked", "error", "pending"].includes(status)) {
        return status;
      }
      return "invalid-route";
    }
    if (data.passed === true) {
      return gateResultsProvePass(data.results) ? "green" : "default";
    }
    return "request-changes";
  } catch {
    return "default";
  }
}

function gateResultsProvePass(results) {
  if (!Array.isArray(results) || results.length === 0) {
    return false;
  }
  let requiredSeen = false;
  for (const result of results) {
    if (!result || typeof result !== "object" || Array.isArray(result)) {
      return false;
    }
    if (result.required === false) {
      continue;
    }
    requiredSeen = true;
    if (
      typeof result.command !== "string"
      || result.command.trim().length === 0
      || !hasGateEvidence(result)
      || !(result.passed === true || result.status === "pass" || result.status === "ok")
    ) {
      return false;
    }
  }
  return requiredSeen;
}

function hasDuplicateJsonObjectKeys(content) {
  const stack = [];
  for (let index = 0; index < content.length;) {
    const character = content[index];
    if (character === '"') {
      const start = index;
      index += 1;
      while (index < content.length) {
        if (content[index] === "\\") {
          index += 2;
          continue;
        }
        if (content[index] === '"') {
          index += 1;
          break;
        }
        index += 1;
      }
      const frame = stack.at(-1);
      if (frame?.type === "object" && frame.expectingKey) {
        const key = JSON.parse(content.slice(start, index));
        if (frame.keys.has(key)) {
          return true;
        }
        frame.keys.add(key);
        frame.expectingKey = false;
      }
      continue;
    }
    if (character === "{") {
      stack.push({ type: "object", expectingKey: true, keys: new Set() });
    } else if (character === "[") {
      stack.push({ type: "array" });
    } else if (character === "}" || character === "]") {
      stack.pop();
    } else if (character === ",") {
      const frame = stack.at(-1);
      if (frame?.type === "object") {
        frame.expectingKey = true;
      }
    }
    index += 1;
  }
  return false;
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

function upsertBootstrapBlock(pathName, label) {
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
- 무메타데이터 project-local skill은 세 host에 설치·노출하되 \`on-demand\`로 유지한다. \`always\`는 \`workflowPhases\`가 없으면 코드 작성·리뷰 phase에만 적용하고, 명시된 경우 해당 phase에만 적용한다. \`conditional\`은 비코드 phase를 포함해 현재 phase와 task/path selector가 모두 매칭될 때만 강제해서 읽는다.
- 프로젝트 skill은 \`skills/<name>/SKILL.md\` 또는 private \`.agent-flow/local-skills/<name>/SKILL.md\`에 둔다.
- install/bootstrap 후 \`.agent-flow/skills/index.json\` metadata를 보고 필요한 skill만 읽는다. 모든 SKILL.md 전문을 항상 읽지 않는다.
- Claude/Codex/OMP 프로젝트 skill 경로는 leader checkout의 install 결과를 따른다. worktree 안에서 install, index 재생성, skill link 재생성을 하지 않는다.
- Claude/Codex/OMP hook이 자동 차단하는 보호 브랜치 commit/push와 leader checkout/switch 금지는 모든 host에서 동일하게 지킨다.

${end}
`;
  const current = fs.existsSync(pathName) ? fs.readFileSync(pathName, "utf8") : "";
  if (current.includes(start) && current.includes(end)) {
    const before = current.slice(0, current.indexOf(start));
    const after = current.slice(current.indexOf(end) + end.length);
    atomicInstallWrite(pathName, `${before}${block}${after.replace(/^\n/, "")}`);
    return;
  }
  const prefix = current.trim() ? `${current.trimEnd()}\n\n` : `# ${label}\n\n`;
  atomicInstallWrite(pathName, `${prefix}${block}`);
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
  atomicInstallWrite(pathName, next);
}

function removeGitignoreEntries(pathName, entries) {
  const metadata = lstatIfExists(pathName);
  if (!metadata) return;
  if (metadata.isSymbolicLink() || !metadata.isFile()) {
    throw new Error(`blocked: gitignore destination is unsafe: ${pathName}`);
  }
  const removable = new Set(entries);
  const current = fs.readFileSync(pathName, "utf8");
  const newline = current.includes("\r\n") ? "\r\n" : "\n";
  const lines = current.split(/\r?\n/);
  const kept = lines.filter((line) => !removable.has(line.trim()));
  if (kept.length === lines.length) return;
  atomicInstallWrite(pathName, kept.join(newline));
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

function bootstrapMarkdown(label) {
  return `# ${label} Agent Flow Bootstrap

Before feature work, run:

\`\`\`bash
${AGENT_FLOW_COMMAND} run "<task>"
\`\`\`

install은 프로젝트당 1회만 수행합니다. 새 세션이 시작됐다는 이유로 install을 다시 실행하지 않습니다.
Follow the CLI output exactly. Git projects start inside \`.agent-flow/worktrees/feat-<slug>/\` without switching the leader branch; continue with the printed \`next_command\`.

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
- 무메타데이터 project-local skill은 세 host에 설치·노출하되 \`on-demand\`로 유지한다. \`always\`는 \`workflowPhases\`가 없으면 코드 작성·리뷰 phase에만 적용하고, 명시된 경우 해당 phase에만 적용한다. \`conditional\`은 비코드 phase를 포함해 현재 phase와 task/path selector가 모두 매칭될 때만 강제해서 읽는다.
- 프로젝트 skill은 \`skills/<name>/SKILL.md\` 또는 private \`.agent-flow/local-skills/<name>/SKILL.md\`에 둔다.
- install/bootstrap 후 \`.agent-flow/skills/index.json\` metadata를 보고 필요한 skill만 읽는다. 모든 SKILL.md 전문을 항상 읽지 않는다.
- Claude/Codex/OMP 프로젝트 skill 경로는 leader checkout의 install 결과를 따른다. worktree 안에서 install, index 재생성, skill link 재생성을 하지 않는다.
- Claude/Codex/OMP hook이 자동 차단하는 보호 브랜치 commit/push와 leader checkout/switch 금지는 모든 host에서 동일하게 지킨다.

During code generation, modification, and code review phases, apply \`code-generation-discipline\`. Resolve required skills from active profile metadata, installed skill index, changed files, and task scope. Load only the touched profile skill union.
For Android/Kotlin/Compose/KMP changes, read matching skills only through the leader checkout's \`.agent-flow/skills/index.json\` project snapshot. React Native \`android/\` native changes also apply the Android profile mapping. Codex, Claude, and OMP must use the same indexed path and tree hash. If a required snapshot is missing or changed, report \`missing local <group>: <skill>\` and do not fall back to a host-global path.
`;
}

function newRunId() {
  const stamp = new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
  return `${stamp}-${Math.random().toString(16).slice(2, 10)}`;
}

function fullFeatureWorkflowYaml() {
  return fullFeatureWorkflow().text;
}

function projectLauncherSource() {
  return `#!/usr/bin/env node
"use strict";

async function main() {
  const fs = await import("node:fs");
  const crypto = await import("node:crypto");
  const path = await import("node:path");
  const { spawnSync } = await import("node:child_process");
  const launcher = fs.realpathSync.native(process.argv[1]);
  const agentFlowRoot = path.dirname(path.dirname(launcher));
  const projectRoot = path.dirname(agentFlowRoot);
  const launcherRelative = ".agent-flow/bin/agent-flow";
  const runtimeRootRelative = ".agent-flow/runtime/node";
  const runtimeRelative = ".agent-flow/runtime/node/bin/agent-flow-kit.mjs";
  const pythonRuntimeRootRelative = ".agent-flow/runtime/python";
  const sha256 = (content) => crypto.createHash("sha256").update(content).digest("hex");
  const compare = (left, right) => {
    const a = Array.from(String(left), (char) => char.codePointAt(0));
    const b = Array.from(String(right), (char) => char.codePointAt(0));
    for (let index = 0; index < Math.min(a.length, b.length); index += 1) {
      if (a[index] !== b[index]) return a[index] - b[index];
    }
    return a.length - b.length;
  };
  const requireDescendant = (relative, kind, label) => {
    let cursor = projectRoot;
    const parts = relative.split("/");
    if (parts.some((part) => !part || part === "." || part === "..")) {
      throw new Error(label + " path is invalid");
    }
    for (let index = 0; index < parts.length; index += 1) {
      cursor = path.join(cursor, parts[index]);
      const metadata = fs.lstatSync(cursor);
      if (metadata.isSymbolicLink()) throw new Error(label + " path may not use symlinks");
      const final = index === parts.length - 1;
      const valid = final
        ? (kind === "file" ? metadata.isFile() : metadata.isDirectory())
        : metadata.isDirectory();
      if (!valid) throw new Error(label + " path has an invalid component");
      if (final && kind === "file" && metadata.nlink !== 1) {
        throw new Error(label + " may not be hard-linked");
      }
    }
    const bounded = path.relative(projectRoot, cursor);
    if (!bounded || bounded.startsWith("..") || path.isAbsolute(bounded)) {
      throw new Error(label + " escapes the project");
    }
    return cursor;
  };
  const managedLauncher = requireDescendant(launcherRelative, "file", "pinned project launcher");
  if (launcher !== managedLauncher) throw new Error("pinned project launcher identity mismatch");
  if (process.platform !== "win32" && (fs.statSync(managedLauncher).mode & 0o111) === 0) {
    throw new Error("pinned project launcher is not executable");
  }
  const kitPath = requireDescendant(".agent-flow/kit.json", "file", "installed kit metadata");
  let kit;
  try {
    kit = JSON.parse(fs.readFileSync(kitPath, "utf8"));
  } catch (error) {
    throw new Error("installed kit metadata is unreadable: " + error.message);
  }
  const contract = kit && kit.project_runtime_contract;
  const launcherContract = contract && contract.launcher;
  const runtimeContract = contract && contract.node_runtime;
  const pythonRuntimeContract = contract && contract.python_runtime;
  const exactKeys = (value, keys) => value
    && typeof value === "object"
    && !Array.isArray(value)
    && JSON.stringify(Object.keys(value).sort(compare)) === JSON.stringify([...keys].sort(compare));
  const isSha256 = (value) => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
  if (
    !exactKeys(contract, ["launcher", "node_runtime", "python_runtime", "version"])
    || contract.version !== 2
    || !exactKeys(launcherContract, ["path", "sha256"])
    || launcherContract.path !== launcherRelative
    || !isSha256(launcherContract.sha256)
    || !exactKeys(runtimeContract, ["entrypoint", "root", "tree_hash"])
    || runtimeContract.root !== runtimeRootRelative
    || runtimeContract.entrypoint !== runtimeRelative
    || !isSha256(runtimeContract.tree_hash)
    || !exactKeys(pythonRuntimeContract, ["root", "tree_hash"])
    || pythonRuntimeContract.root !== pythonRuntimeRootRelative
    || !isSha256(pythonRuntimeContract.tree_hash)
  ) {
    throw new Error("installed project runtime contract is invalid");
  }
  const normalized = {
    version: 2,
    launcher: { path: launcherRelative, sha256: launcherContract.sha256 },
    node_runtime: {
      root: runtimeRootRelative,
      entrypoint: runtimeRelative,
      tree_hash: runtimeContract.tree_hash,
    },
    python_runtime: {
      root: pythonRuntimeRootRelative,
      tree_hash: pythonRuntimeContract.tree_hash,
    },
  };
  const commitment = sha256(Buffer.from(JSON.stringify({ version: 2, contract: normalized }), "utf8"));
  if (
    kit.project_runtime_contract_commitment_version !== 2
    || !isSha256(kit.project_runtime_contract_commitment)
    || commitment !== kit.project_runtime_contract_commitment
  ) {
    throw new Error("installed project runtime commitment is invalid");
  }
  if (
    !exactKeys(kit.node_runtime, ["path", "tree_hash"])
    || kit.node_runtime.path !== runtimeRelative
    || kit.node_runtime.tree_hash !== runtimeContract.tree_hash
  ) {
    throw new Error("installed Node runtime compatibility metadata is invalid");
  }
  if (
    !exactKeys(kit.python_runtime, ["path", "tree_hash"])
    || kit.python_runtime.path !== pythonRuntimeRootRelative
    || kit.python_runtime.tree_hash !== pythonRuntimeContract.tree_hash
  ) {
    throw new Error("installed Python runtime compatibility metadata is invalid");
  }
  if (sha256(fs.readFileSync(managedLauncher)) !== launcherContract.sha256) {
    throw new Error("pinned project launcher changed after install");
  }
  const runtimeRoot = requireDescendant(runtimeRootRelative, "directory", "pinned Node runtime root");
  const runtime = requireDescendant(runtimeRelative, "file", "pinned Node runtime entrypoint");
  const hashRuntimeTree = (root, label) => {
    const files = [];
    const visit = (directory) => {
      for (const name of fs.readdirSync(directory).sort(compare)) {
        const candidate = path.join(directory, name);
        const metadata = fs.lstatSync(candidate);
        if (metadata.isSymbolicLink()) throw new Error(label + " may not contain symlinks");
        if (metadata.isDirectory()) visit(candidate);
        else if (metadata.isFile()) {
          if (metadata.nlink !== 1) throw new Error(label + " may not contain hard-linked files");
          files.push(candidate);
        } else throw new Error(label + " may contain only regular files");
      }
    };
    visit(root);
    files.sort(compare);
    const digest = crypto.createHash("sha256");
    for (const file of files) {
      digest.update(path.relative(root, file).split(path.sep).join("/"));
      digest.update("\\0");
      digest.update(fs.readFileSync(file));
      digest.update("\\0");
    }
    return digest.digest("hex");
  };
  if (hashRuntimeTree(runtimeRoot, "pinned Node runtime") !== runtimeContract.tree_hash) {
    throw new Error("pinned Node runtime changed after install");
  }
  const pythonRuntimeRoot = requireDescendant(
    pythonRuntimeRootRelative,
    "directory",
    "pinned Python runtime root",
  );
  if (hashRuntimeTree(pythonRuntimeRoot, "pinned Python runtime") !== pythonRuntimeContract.tree_hash) {
    throw new Error("pinned Python runtime changed after install");
  }
  const result = spawnSync(process.execPath, [runtime, ...process.argv.slice(2)], {
    cwd: process.cwd(),
    env: process.env,
    stdio: "inherit",
  });
  if (result.error) throw result.error;
  process.exit(result.status ?? 1);
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
});
`;
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

## Behavior

- Treat \`/agent-flow\` as a project-local workflow trigger, not as a shell path.
- Keep git-project runtime state private under the repository git dir, such as \`.git/agent-flow/worktrees/feat-<slug>/\`; expose it only for status, debugging, or artifact inspection.
- On a new session, always check \`${AGENT_FLOW_COMMAND} status\` first and continue from that result.
- After a phase writes its artifact, run the \`next_command\` printed by status or the current phase output.
- If the workflow pauses for design or slice review, summarize the relevant artifact and wait for user approval before continuing.
- A paused run advances only through the printed \`--approve-pause\` command after approval; repeating the plain command stays blocked.
- During code generation, modification, and code review phases, apply \`code-generation-discipline\`. Resolve required skills from active profile metadata, installed skill index, changed files, and task scope. Load only the touched profile skill union. If a required local skill is missing, report it and wait for install or explicit override.
- Keep user-facing replies short Korean by default. Keep code, commands, paths, and identifiers in English.
- Do not paste long logs or whole files. Summarize only current phase, action, \`next_command\`, and blocker when useful.
`;
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
  return `---\nname: architecture-reviewer\ndescription: Use during the full-feature architecture-review phase.\n---\n\n# Architecture Reviewer\n\nUse during the full-feature architecture-review phase.\n\nReview implemented code against domain decisions and DDD/Clean Architecture. Set \`host-parity-check: pass\` only after Claude, Codex, and OMP workflow routing and hook behavior are all verified as equivalent for this change. Run two independent active-host reviewer sub-agents before approve. Each reviewer section must include \`reviewer-source: sub-agent\`; optional cross-host reviewers are extra evidence and do not replace active-host reviewers.\n\nArtifact template:\n\n# Architecture Review\n\n## Reviewer 1\nreviewer-source: sub-agent\nverdict: approve | request-changes\n\n## Findings\n\n## Domain Alignment\n\n## Layer Violations\n\n## Repository Boundary Issues\n\n## Dependency Direction Issues\n\n## Required Refactors\n\n## Approved Exceptions\n\n## Reviewer 2\nreviewer-source: sub-agent\nverdict: approve | request-changes\n\n## Findings\n\n## Overall\nverdict: approve | request-changes\n\n## Completion Gate\nskills_checked: true\nprofile-skill-selection: applied\nactive-profiles: <profile list>\nchanged-file-skill-resolution: applied\nrequired-profile-skills: checked\nmissing-required-profile-skills: none|<list>\narchitecture-contract-check: pass|fail|n/a\nhost-parity-check: pass|fail\nhook-parity-check: pass|fail\nclean-architecture: applied\nproject-local-skills: checked|n/a\nproject-local-skills-used: <skill list or n/a>\ndependency-rule: pass|fail\nusecase-boundary: pass|fail|n/a\nusecase-calls-usecase: pass|fail\nrepository-boundary: pass|fail\ncache-boundary: pass|fail|n/a\nmemory-disk-cache-separated: pass|fail|n/a\nmapping-boundary: pass|fail|n/a\ndto-entity-domain-ui-separated: pass|fail\nsolid-boundary-check: pass|fail\npresentation-skill: android|react|react-native|ios|n/a\npresentation-state-review: pass|fail|n/a\nui-state-modeling: explicit|n/a\npresentation-mapping-boundary: domain-to-uimodel|n/a\ndi-boundary: hilt|context-provider|tsyringe|swift-environment|factory|swift-dependencies|swinject|needle|direct|existing|n/a\n`;
}

function pushWatchSkillMarkdown() {
  return `---\nname: push-watch\ndescription: Use this skill after local verification is complete and the branch is ready to publish.\n---\n\n# Push Watch\n\nUse this skill after local verification is complete and the branch is ready to publish.\n\nRun:\n\n\`\`\`bash\n${AGENT_FLOW_COMMAND} run push-watch\n\`\`\`\n\nFlow:\n\n1. Sanity check the branch and working tree.\n2. Commit and push the current branch.\n3. Open or record the pull request.\n4. Watch PR checks and review threads.\n5. Route failures through \`pr-comment-fix\` or \`pr-ci-fix\`; comment fixes must also resolve the corresponding GitHub review threads.\n6. Push again and return to \`pr-watch\`.\n7. When checks and comments are green, route to \`merge\`.\n\nRules:\n\n- Protected branches are blocked: main, master, develop.\n- Record PR watch state with \`status: green\`, \`status: comments\`, \`status: ci-failed\`, or \`status: pending\`.\n- merge requires explicit approval. Do not merge unattended.\n`;
}

function pushWatchPromptMarkdown() {
  return `# push-watch\n\nCommit, push, open a PR, and start the PR watch loop.\n\nUse \`${AGENT_FLOW_COMMAND} run push-watch\`.\n\nDo not run on protected branches. Do not merge without explicit approval.\n`;
}

function pushWatchTickPromptMarkdown() {
  return `# push-watch-tick\n\nPoll the current PR checks and review threads.\n\nUse \`${AGENT_FLOW_COMMAND} run push-watch-tick\`.\n\nWrite \`artifacts/pr-watch.md\` with one status line: \`status: green\`, \`status: comments\`, \`status: ci-failed\`, or \`status: pending\`.\n`;
}

function phasePrompt(phase, root = null, installedProfileBlock = null) {
  const markers = phase.required_markers?.length
    ? `\n\n## Completion markers\n\nThe runner blocks this phase until the artifact includes a \`## Completion Gate\` section with these marker lines:\n\n${phase.required_markers.map((marker) => `- \`${marker}\``).join("\n")}\n\nWrite the heading and completed marker lines as normal, unfenced Markdown. Do not place them inside a fenced or indented code block; replace every open value or choice with one concrete accepted value.\n`
    : "";
  const profileBlock = root
    ? installedProfileBlock ?? installedProfilePromptBlock(root, phase.id)
    : "";
  const pauseBlock = phase.pause_after
    ? "\n\nThis phase has `pause_after: true`. The first validated advance records the pause. Wait for explicit user approval before advancing again."
    : "";
  return `# ${phase.id}\n\n${phase.instruction}${profileBlock}${markers}${pauseBlock}\n\nResolve profile and project-local skills from the active run status before acting.\n\nSave the required artifact before running:\n\n\`\`\`bash\n${AGENT_FLOW_COMMAND} run advance\n\`\`\`\n`;
}

function shellQuote(value) {
  return `'${String(value).replaceAll("'", "'\\''")}'`;
}

const MANAGED_HOOK_VERIFIER = [
  "import base64,hashlib,os,stat,sys,tempfile,time",
  "p=base64.b64decode(sys.argv[1],validate=True).decode('utf-8'); expected=sys.argv[2]",
  "source_fd=None; stage_fd=None; stage_path=None",
  "try:",
  " source_fd=os.open(p,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))",
  " before=os.fstat(source_fd)",
  " ok=stat.S_ISREG(before.st_mode) and before.st_nlink==1 and bool(before.st_mode & 0o111)",
  " if ok:",
  "  with os.fdopen(os.dup(source_fd),'rb') as f: content=f.read()",
  "  after=os.fstat(source_fd)",
  "  identity=(before.st_dev,before.st_ino,before.st_mode,before.st_nlink,before.st_size)",
  "  ok=identity==(after.st_dev,after.st_ino,after.st_mode,after.st_nlink,after.st_size) and hashlib.sha256(content).hexdigest()==expected",
  " if not ok: raise OSError('integrity mismatch')",
  " stage_fd,stage_path=tempfile.mkstemp(prefix='agent-flow-managed-hook-')",
  " view=memoryview(content)",
  " while view:",
  "  written=os.write(stage_fd,view)",
  "  if written<=0: raise OSError('staging write failed')",
  "  view=view[written:]",
  " os.fsync(stage_fd); os.fchmod(stage_fd,0o400); os.lseek(stage_fd,0,os.SEEK_SET)",
  " os.unlink(stage_path); stage_path=None; os.set_inheritable(stage_fd,True)",
  " os.close(source_fd); source_fd=None",
  " source_ref=f'/dev/fd/{stage_fd}'",
  " if not os.path.exists(source_ref): raise OSError('descriptor execution unavailable')",
  " env=dict(os.environ); env['AGENT_FLOW_MANAGED_HOOK_SOURCE_PATH']=p",
  " try: hold=int(env.get('AGENT_FLOW_TEST_HOLD_AFTER_MANAGED_HOOK_STAGE_MS','0'))",
  " except ValueError: hold=0",
  " if 0<hold<=10000:",
  "  print('agent-flow:test-hook-staged:'+os.path.basename(p),file=sys.stderr,flush=True); time.sleep(hold/1000)",
  " if p.endswith('.py'): os.execve('/usr/bin/python3',['/usr/bin/python3','-I',source_ref],env)",
  " if p.endswith('.sh'): os.execve('/bin/bash',['/bin/bash',source_ref],env)",
  " raise OSError('unsupported managed hook type')",
  "except (OSError,UnicodeError,ValueError):",
  " print('agent-flow: blocked because managed hook integrity validation failed',file=sys.stderr)",
  " raise SystemExit(2)",
  "finally:",
  " if source_fd is not None: os.close(source_fd)",
  " if stage_path is not None:",
  "  try: os.unlink(stage_path)",
  "  except OSError: pass",
  " if stage_fd is not None:",
  "  try: os.close(stage_fd)",
  "  except OSError: pass",
].join("\n");

function hookScriptCommand(root, scriptName) {
  const scriptPath = path.join(root, ".agent-flow", "scripts", "hooks", scriptName);
  const digest = sha256Bytes(fs.readFileSync(scriptPath));
  return [
    shellQuote("/usr/bin/python3"),
    "-I",
    "-c",
    shellQuote(MANAGED_HOOK_VERIFIER),
    shellQuote(Buffer.from(scriptPath, "utf8").toString("base64")),
    shellQuote(digest),
  ].join(" ");
}

function managedHookVerifierDetails(command) {
  if (typeof command !== "string") return null;
  const prefix = `${shellQuote("/usr/bin/python3")} -I -c ${shellQuote(MANAGED_HOOK_VERIFIER)} `;
  if (!command.startsWith(prefix)) return null;
  const match = command.slice(prefix.length).match(/^'([A-Za-z0-9+/=]+)' '([0-9a-f]{64})'$/);
  if (!match) return null;
  let decodedPath;
  try {
    decodedPath = Buffer.from(match[1], "base64");
  } catch {
    return null;
  }
  if (decodedPath.toString("base64") !== match[1]) return null;
  const scriptPath = decodedPath.toString("utf8");
  if (!Buffer.from(scriptPath, "utf8").equals(decodedPath)) return null;
  return { scriptPath, sha256: match[2] };
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
  if (typeof command !== "string") return null;
  const verifier = managedHookVerifierDetails(command);
  if (verifier) {
    const name = path.basename(verifier.scriptPath);
    if (["guard-worktree.sh", "guard-worktree-write.py", "guard-protected-branch.sh", "show-phase-status.sh", "comment-checker.py"].includes(name)) {
      return name;
    }
  }
  for (const match of command.matchAll(/(?:^|[\s'"])([A-Za-z0-9+/]+={0,2})(?=$|[\s'"])/g)) {
    const encoded = match[1];
    const decoded = Buffer.from(encoded, "base64");
    if (decoded.toString("base64") === encoded) {
      const decodedPath = decoded.toString("utf8");
      if (Buffer.from(decodedPath, "utf8").equals(decoded)) {
        const name = path.basename(decodedPath);
        if (["guard-worktree.sh", "guard-worktree-write.py", "guard-protected-branch.sh", "show-phase-status.sh", "comment-checker.py"].includes(name)) {
          return name;
        }
      }
    }
  }
  if (
    command.includes("AGENT_FLOW_MANAGED_HOOK_SOURCE_PATH")
    && command.includes("agent-flow-managed-hook-")
    && command.includes("descriptor execution unavailable")
  ) return "__managed-verifier__";
  const normalized = unquoteShellWord(command).replaceAll("\\", "/").replaceAll("'", "").replaceAll('"', "");
  for (const scriptName of ["guard-worktree.sh", "guard-worktree-write.py", "guard-protected-branch.sh", "show-phase-status.sh", "comment-checker.py"]) {
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

function trustedManagedHookScriptName(root, command, expectedScriptHashes = null) {
  const normalizedRoot = path.resolve(root).replaceAll("\\", "/");
  const verifier = managedHookVerifierDetails(command);
  for (const scriptName of ["guard-worktree.sh", "guard-worktree-write.py", "guard-protected-branch.sh", "show-phase-status.sh", "comment-checker.py"]) {
    const expected = `${normalizedRoot}/.agent-flow/scripts/hooks/${scriptName}`;
    const relative = `.agent-flow/scripts/hooks/${scriptName}`;
    if (!verifier || verifier.scriptPath.replaceAll("\\", "/") !== expected) continue;
    const metadata = lstatIfExists(verifier.scriptPath);
    if (!metadata?.isFile() || metadata.isSymbolicLink()) continue;
    const expectedSha = expectedScriptHashes instanceof Map
      ? expectedScriptHashes.get(relative)
      : sha256Bytes(fs.readFileSync(verifier.scriptPath));
    if (typeof expectedSha === "string" && expectedSha === verifier.sha256) return scriptName;
  }
  return null;
}

const WRITE_TOOL_MATCHER = "^(apply_patch|Write|Edit|MultiEdit|NotebookEdit|Eval|Python|Notebook|write|edit|multi_edit|multiedit|notebook_edit|notebookedit|eval|python|notebook)$";

function codexHooksSettings(root) {
  return {
    hooks: {
      PreToolUse: [
        {
          matcher: "Bash",
          hooks: [
            { type: "command", command: hookScriptCommand(root, "guard-worktree.sh") },
            { type: "command", command: hookScriptCommand(root, "guard-protected-branch.sh") },
            { type: "command", command: hookScriptCommand(root, "guard-worktree-write.py") },
          ],
        },
        {
          matcher: WRITE_TOOL_MATCHER,
          hooks: [{ type: "command", command: hookScriptCommand(root, "guard-worktree-write.py") }],
        },
      ],
      PostToolUse: [
        {
          matcher: WRITE_TOOL_MATCHER,
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

function removeManagedHookCommands(settings) {
  if (!settings?.hooks || typeof settings.hooks !== "object" || Array.isArray(settings.hooks)) {
    return;
  }
  for (const [event, entries] of Object.entries(settings.hooks)) {
    if (!Array.isArray(entries)) continue;
    settings.hooks[event] = entries.flatMap((entry) => {
      if (!entry || typeof entry !== "object" || !Array.isArray(entry.hooks)) return [entry];
      const hooks = entry.hooks.filter((hook) => !managedHookScriptName(hook?.command));
      if (hooks.length === 0 && entry.hooks.length > 0) return [];
      return [{ ...entry, hooks }];
    });
  }
}

function readHookSettings(settingsPath) {
  if (!fs.existsSync(settingsPath)) {
    return {};
  }
  try {
    return JSON.parse(fs.readFileSync(settingsPath, "utf8"));
  } catch {
    const backupPath = uniqueHookSettingsBackupPath(settingsPath);
    fs.copyFileSync(settingsPath, backupPath, fs.constants.COPYFILE_EXCL);
    console.error(`warning: could not parse ${settingsPath}; backed up to ${backupPath} before overwriting`);
    return {};
  }
}

function uniqueHookSettingsBackupPath(settingsPath) {
  for (let attempt = 0; attempt < 32; attempt += 1) {
    const suffix = `${Date.now()}-${process.pid}-${crypto.randomBytes(6).toString("hex")}`;
    const candidate = `${settingsPath}.bak-${suffix}`;
    if (!lstatIfExists(candidate)) return candidate;
  }
  throw new Error(`blocked: could not allocate a unique hook settings backup: ${settingsPath}`);
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

function upsertTomlValue(text, tableHeader, key, value) {
  const tableName = tableHeader.slice(1, -1);
  const eofTablePattern = new RegExp(
    `(^|\\n)[ \\t]*\\[[ \\t]*${escapeRegex(tableName)}[ \\t]*\\][ \\t]*(?:#[^\\n]*)?$`,
  );
  if (eofTablePattern.test(text)) {
    return {
      text: `${text}\n${key} = ${value}\n`,
      tableMatched: true,
      keyMatched: false,
    };
  }
  const tablePattern = new RegExp(`(^|\\n)\\s*\\[\\s*${escapeRegex(tableName)}\\s*\\]\\s*(?:#.*)?\\n([\\s\\S]*?)(?=\\n\\s*\\[[^\\n]+\\]|$)`);
  const keyPattern = new RegExp(`(^|\\n)\\s*${escapeRegex(key)}\\s*=.*(?=\\n|$)`);
  const match = text.match(tablePattern);
  if (!match) {
    const prefix = text.trim() ? `${text.replace(/\n*$/, "\n\n")}` : "";
    return {
      text: `${prefix}${tableHeader}\n${key} = ${value}\n`,
      tableMatched: false,
      keyMatched: false,
    };
  }
  const keyMatched = keyPattern.test(match[2]);
  return {
    text: text.replace(tablePattern, (full, leading, body) => {
      const nextBody = keyMatched
      ? body.replace(keyPattern, `$1${key} = ${value}`)
      : `${body.replace(/\n*$/, "")}\n${key} = ${value}\n`;
      return `${leading}${tableHeader}\n${nextBody}`;
    }),
    tableMatched: true,
    keyMatched,
  };
}

function inspectCodexToml(text, updates) {
  const python = preferredCodexTomlPython();
  const script = String.raw`
import json
import sys
try:
    import tomllib as toml
except ImportError:
    import tomli as toml
payload = json.load(sys.stdin)
try:
    document = toml.loads(payload["text"])
except Exception:
    raise SystemExit(2)
results = []
for update in payload.get("updates", []):
    cursor = document
    table_exists = True
    for segment in update["tablePath"]:
        if not isinstance(cursor, dict) or segment not in cursor:
            table_exists = False
            break
        cursor = cursor[segment]
    table_exists = table_exists and isinstance(cursor, dict)
    results.append({
        "table_exists": table_exists,
        "key_exists": table_exists and update["key"] in cursor,
        "value_matches": (
            table_exists
            and update["key"] in cursor
            and cursor[update["key"]] == update.get("expectedValue")
        ),
    })
print(json.dumps({"targets": results}, separators=(",", ":")))
`;
  const payload = {
    text,
    updates: updates.map((update) => ({
      tablePath: update.tablePath,
      key: update.key,
      expectedValue: update.expectedValue,
    })),
  };
  const result = spawnSync(python, ["-c", script], {
    encoding: "utf8",
    input: JSON.stringify(payload),
    timeout: 5000,
    maxBuffer: 2 * 1024 * 1024,
  });
  if (result.error || result.status !== 0) {
    throw new Error("blocked: Codex config is not valid TOML");
  }
  let parsed;
  try {
    parsed = JSON.parse(result.stdout);
  } catch {
    throw new Error("blocked: Codex config TOML validator returned invalid output");
  }
  if (!parsed || !Array.isArray(parsed.targets) || parsed.targets.length !== updates.length) {
    throw new Error("blocked: Codex config TOML validator returned invalid targets");
  }
  return parsed.targets;
}

function preferredCodexTomlPython() {
  const forced = process.env.AGENT_FLOW_TEST_CODEX_TOML_PYTHON?.trim();
  const virtualEnvPython = process.env.VIRTUAL_ENV
    ? path.join(process.env.VIRTUAL_ENV, process.platform === "win32" ? "Scripts/python.exe" : "bin/python")
    : null;
  const kitVenvPython = path.join(
    KIT_ROOT,
    ".venv",
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
  );
  const leaderRoot = resolveManagedWorktreeRoot(KIT_ROOT);
  const leaderVenvPython = leaderRoot
    ? path.join(
        leaderRoot,
        ".venv",
        process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
      )
    : null;
  const candidates = forced
    ? [forced]
    : [
        process.env.PYTHON,
        process.env.PYTHON_EXECUTABLE,
        virtualEnvPython,
        fs.existsSync(kitVenvPython) ? kitVenvPython : null,
        leaderVenvPython && fs.existsSync(leaderVenvPython) ? leaderVenvPython : null,
        "python3.14",
        "python3.13",
        "python3.12",
        "python3.11",
        "python3.10",
        "python3",
        "python",
      ].filter(Boolean);
  const probe = [
    "import importlib.util, sys",
    "sys.exit(0 if importlib.util.find_spec('tomllib') or importlib.util.find_spec('tomli') else 1)",
  ].join(";");
  for (const candidate of [...new Set(candidates)]) {
    const result = spawnSync(candidate, ["-c", probe], {
      stdio: "ignore",
      timeout: 3000,
    });
    if (!result.error && result.status === 0) return candidate;
  }
  throw new Error("blocked: no Python TOML parser is available for Codex config validation");
}

function codexConfigPath() {
  const configuredHome = process.env.CODEX_HOME?.trim();
  if (configuredHome) {
    return path.join(path.resolve(configuredHome), "config.toml");
  }
  if (!HOME) {
    return null;
  }
  return path.join(path.resolve(HOME), ".codex", "config.toml");
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

function queryCodexProjectHooks(root) {
  const codexBinary = resolveCodexBinary();
  if (!codexBinary) {
    return { hooks: [], errors: ["Codex CLI unavailable"] };
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
    .map((hook) => ({
      key: hook.key,
      trustedHash: hook.currentHash,
      trustStatus: hook.trustStatus,
      command: hook.command ?? "",
    }));
  console.log(JSON.stringify({ hooks, errors: Array.isArray(entry?.errors) ? entry.errors : [] }));
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
    return { hooks: [], errors: ["Codex hook discovery failed"] };
  }
  try {
    const parsed = JSON.parse(result.stdout.trim());
    return parsed && typeof parsed === "object" && Array.isArray(parsed.hooks) && Array.isArray(parsed.errors)
      ? parsed
      : { hooks: [], errors: ["Codex hook discovery returned an invalid schema"] };
  } catch {
    return { hooks: [], errors: ["Codex hook discovery returned invalid JSON"] };
  }
}

function prepareCodexTrustState(root, { required = false } = {}) {
  if (process.env.AGENT_FLOW_SKIP_CODEX_TRUST === "1") {
    return null;
  }
  const before = readInstalledKit(root);
  assertManagedHookContractInstalled(root, before);
  const discovery = queryCodexProjectHooks(root);
  const trustHoldMs = Number.parseInt(process.env.AGENT_FLOW_TEST_HOLD_AFTER_CODEX_TRUST_QUERY_MS ?? "0", 10);
  if (Number.isInteger(trustHoldMs) && trustHoldMs > 0 && trustHoldMs <= 10_000) {
    fs.writeFileSync(path.join(root, ".agent-flow", "trust-query-ready"), "ready\n", "utf8");
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, trustHoldMs);
  }
  const after = readInstalledKit(root);
  assertManagedHookContractInstalled(root, after);
  if (
    before.managed_hook_contract_commitment !== after.managed_hook_contract_commitment
    || before.skill_plan_hash !== after.skill_plan_hash
  ) {
    throw new Error("blocked: managed hook commitment changed during Codex trust query");
  }
  if (discovery.errors.length > 0) {
    throw new Error("blocked: Codex hook discovery reported configuration errors");
  }
  const managedHooks = exactManagedCodexHooksForTrust(
    root,
    discovery.hooks,
    new Map(
      normalizedManagedHookContract(after.managed_hook_contract).scripts
        .map(([relative, committedSha]) => [relative, committedSha]),
    ),
  );
  if (managedHooks.length === 0) {
    if (required) {
      throw new Error("blocked: active Codex hook trust registration returned no managed project hooks");
    }
    console.error("warning: Codex hook trust not registered; codex app-server did not return project hooks");
    return null;
  }
  const configPath = codexConfigPath();
  if (!configPath) {
    if (required) {
      throw new Error("blocked: active Codex project trust registration requires CODEX_HOME or HOME");
    }
    console.error("warning: Codex project trust not registered; HOME is unavailable");
    return null;
  }
  const canonicalRoot = fs.realpathSync.native(root);
  const projectHeader = `[projects."${tomlBasicString(canonicalRoot)}"]`;
  const updates = [{
    tableHeader: projectHeader,
    tablePath: ["projects", canonicalRoot],
    key: "trust_level",
    value: "\"trusted\"",
    expectedValue: "trusted",
  }];
  for (const hook of managedHooks) {
    updates.push({
      tableHeader: `[hooks.state."${tomlBasicString(hook.key)}"]`,
      tablePath: ["hooks", "state", hook.key],
      key: "trusted_hash",
      value: `"${tomlBasicString(hook.trustedHash)}"`,
      expectedValue: hook.trustedHash,
    });
  }
  return {
    version: CODEX_TRUST_OBLIGATION_VERSION,
    configPath: canonicalCodexConfigPath(configPath),
    updates,
    managedHookContractCommitment: after.managed_hook_contract_commitment,
    skillPlanHash: after.skill_plan_hash,
    expectedHooks: managedHooks.map((hook) => ({
      key: hook.key,
      trustedHash: hook.trustedHash,
    })),
  };
}

function applyCodexTrustState(
  root,
  plan,
  { required = false, enableTestFaults = true } = {},
) {
  if (!plan) return false;
  const installed = readInstalledKit(root);
  assertManagedHookContractInstalled(root, installed);
  if (
    installed.managed_hook_contract_commitment !== plan.managedHookContractCommitment
    || installed.skill_plan_hash !== plan.skillPlanHash
  ) {
    throw new Error("blocked: managed hook commitment changed before Codex trust publish");
  }
  const result = applyCodexConfigTrustUpdates({
    leaderRoot: root,
    configPath: plan.configPath,
    updates: plan.updates,
    beforePublish: () => {
      const holdMs = enableTestFaults
        ? Number.parseInt(
            process.env.AGENT_FLOW_TEST_HOLD_BEFORE_CODEX_CONFIG_RENAME_MS ?? "0",
            10,
          )
        : 0;
      if (Number.isInteger(holdMs) && holdMs > 0 && holdMs <= 10_000) {
        fs.writeFileSync(
          path.join(root, ".agent-flow", "codex-config-rename-ready"),
          "ready\n",
          "utf8",
        );
        Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, holdMs);
      }
    },
    afterPublish: () => {
      if (
        enableTestFaults
        && process.env.AGENT_FLOW_TEST_CRASH_AFTER_CODEX_TRUST_APPLY === "1"
      ) {
        process.exit(87);
      }
      verifyPublishedCodexTrustState(root, plan);
    },
    beforeCleanup: () => {
      if (
        enableTestFaults
        && process.env.AGENT_FLOW_TEST_CRASH_AFTER_CODEX_TRUST_COMMIT === "1"
      ) {
        process.exit(88);
      }
    },
  });
  if (required && !result) {
    throw new Error("blocked: active Codex project trust registration failed");
  }
  return result;
}

function verifyPublishedCodexTrustState(root, plan) {
  const installed = readInstalledKit(root);
  assertManagedHookContractInstalled(root, installed);
  if (
    installed.managed_hook_contract_commitment !== plan.managedHookContractCommitment
    || installed.skill_plan_hash !== plan.skillPlanHash
  ) {
    throw new Error("blocked: managed hook commitment changed during Codex trust verification");
  }
  const discovery = queryCodexProjectHooks(root);
  if (discovery.errors.length > 0) {
    throw new Error("blocked: Codex hook verification reported configuration errors");
  }
  const hooks = exactManagedCodexHooksForTrust(
    root,
    discovery.hooks,
    new Map(
      normalizedManagedHookContract(installed.managed_hook_contract).scripts
        .map(([relative, committedSha]) => [relative, committedSha]),
    ),
  );
  const actual = new Map(hooks.map((hook) => [hook.key, hook]));
  for (const expected of plan.expectedHooks ?? []) {
    const hook = actual.get(expected.key);
    if (
      !hook
      || hook.trustedHash !== expected.trustedHash
      || hook.trustStatus !== "trusted"
    ) {
      throw new Error(`blocked: Codex hook trust verification failed: ${expected.key}`);
    }
  }
}

function exactManagedCodexHooksForTrust(root, hooks, expectedScriptHashes) {
  if (!Array.isArray(hooks) || hooks.length === 0) return [];
  if (!(expectedScriptHashes instanceof Map)) {
    throw new Error("blocked: managed hook script provenance is unavailable");
  }
  const expectedNames = expectedManagedHookProjection().map((row) => row[3]).sort(compareCodePoints);
  const managed = [];
  const seenKeys = new Set();
  for (const hook of hooks) {
    if (!hook || typeof hook !== "object" || Array.isArray(hook)) continue;
    const scriptName = trustedManagedHookScriptName(
      root,
      hook.command,
      expectedScriptHashes,
    );
    if (!scriptName) continue;
    if (
      typeof hook.key !== "string"
      || !hook.key
      || seenKeys.has(hook.key)
      || typeof hook.trustedHash !== "string"
      || !/^sha256:[0-9a-f]{64}$/.test(hook.trustedHash)
      || typeof hook.trustStatus !== "string"
    ) {
      throw new Error("blocked: Codex returned invalid managed hook trust metadata");
    }
    seenKeys.add(hook.key);
    managed.push({ ...hook, scriptName });
  }
  const actualNames = managed.map((hook) => hook.scriptName).sort(compareCodePoints);
  if (JSON.stringify(actualNames) !== JSON.stringify(expectedNames)) {
    throw new Error("blocked: Codex returned an incomplete or duplicate managed hook set");
  }
  return managed;
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
  removeManagedHookCommands(settings);
  mergeHookSettings(settings, codexHooksSettings(root).hooks);
  for (const settingsPath of settingsPaths) {
    fs.mkdirSync(path.dirname(settingsPath), { recursive: true });
    atomicInstallWrite(settingsPath, `${JSON.stringify(settings, null, 2)}\n`);
  }
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
            { type: "command", command: hookScriptCommand(root, "guard-worktree-write.py") },
          ],
        },
        {
          matcher: WRITE_TOOL_MATCHER,
          hooks: [{ type: "command", command: hookScriptCommand(root, "guard-worktree-write.py") }],
        },
      ],
      PostToolUse: [
        {
          matcher: WRITE_TOOL_MATCHER,
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
  removeManagedHookCommands(settings);
  mergeHookSettings(settings, claudeHooksSettings(root).hooks);
  fs.mkdirSync(path.dirname(settingsPath), { recursive: true });
  atomicInstallWrite(settingsPath, `${JSON.stringify(settings, null, 2)}\n`);
}

function ompHooksExtensionSource(root) {
  const hookHashes = Object.fromEntries(
    MANAGED_HOOK_SCRIPT_NAMES.map((scriptName) => [
      scriptName,
      sha256Bytes(fs.readFileSync(path.join(KIT_ROOT, "scripts", "hooks", scriptName))),
    ]),
  );
  return String.raw`import crypto from "node:crypto";
import fs from "node:fs";
import { spawn } from "node:child_process";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

process.env.AGENT_FLOW_HOST ||= "omp";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const HOOK_DIR = path.join(ROOT, ".agent-flow", "scripts", "hooks");
const HOOK_SHA256 = ${JSON.stringify(hookHashes)};
const WRITE_TOOL_RE = /^(apply_patch|Write|Edit|MultiEdit|NotebookEdit|Eval|Python|Notebook|write|edit|multi_edit|multiedit|notebook_edit|notebookedit|eval|python|notebook)$/;

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
    const isWrite = WRITE_TOOL_RE.test(String(event?.toolName || ""));
    if (!isBashTool(event?.toolName) && !isWrite) {
      return;
    }
    const payload = hookPayload(event, ctx);
    const scripts = isWrite
      ? ["guard-worktree-write.py"]
      : ["guard-worktree.sh", "guard-protected-branch.sh", "guard-worktree-write.py"];
    for (const scriptName of scripts) {
      const result = await runHook(scriptName, payload, ctx);
      if (result.block) {
        return { block: true, reason: result.reason };
      }
    }
  });

  pi.on("tool_result", async (event, ctx) => {
    if (event?.isError || !WRITE_TOOL_RE.test(String(event?.toolName || ""))) {
      return;
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

  pi.on("session_stop", async (_event, ctx) => {
    const result = await runHook("show-phase-status.sh", { hook_event_name: "session_stop" }, ctx);
    const message = parseSystemMessage(result.reason);
    if (!message) {
      return;
    }
    if (message && ctx?.hasUI && typeof ctx.ui?.notify === "function") {
      await ctx.ui.notify(message, "info");
      return;
    }
    process.stderr.write(message + "\n");
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


function isBashTool(toolName) {
  return /^(Bash|bash)$/.test(String(toolName || ""));
}

async function runHook(scriptName, payload, ctx) {
  const scriptPath = path.join(HOOK_DIR, scriptName);
  let stagedFd = null;
  try {
    stagedFd = stageHookBytes(scriptPath, HOOK_SHA256[scriptName]);
  } catch {
    return { block: true, reason: "agent-flow hook integrity validation failed: " + scriptName };
  }
  const holdMs = Number.parseInt(process.env.AGENT_FLOW_TEST_HOLD_AFTER_MANAGED_HOOK_STAGE_MS || "0", 10);
  if (Number.isInteger(holdMs) && holdMs > 0 && holdMs <= 10000) {
    process.stderr.write("agent-flow:test-hook-staged:" + scriptName + "\n");
    await new Promise((resolve) => setTimeout(resolve, holdMs));
  }
  let result;
  try {
    result = await spawnHook(stagedFd, scriptName, scriptPath, JSON.stringify(payload), ctx?.cwd || ROOT);
  } finally {
    fs.closeSync(stagedFd);
  }
  const reason = (result.stderr || result.stdout || "").trim();
  if (result.status === 0) {
    return { block: false, reason };
  }
  return { block: true, reason: reason || "agent-flow hook blocked: " + scriptName };
}

function stageHookBytes(scriptPath, expectedSha256) {
  const sourceFd = fs.openSync(scriptPath, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
  let content;
  try {
    const before = fs.fstatSync(sourceFd);
    content = fs.readFileSync(sourceFd);
    const after = fs.fstatSync(sourceFd);
    const identity = (value) => [value.dev, value.ino, value.mode, value.nlink, value.size];
    const actual = crypto.createHash("sha256").update(content).digest("hex");
    if (
      !before.isFile()
      || before.nlink !== 1
      || (before.mode & 0o111) === 0
      || JSON.stringify(identity(before)) !== JSON.stringify(identity(after))
      || actual !== expectedSha256
    ) throw new Error("managed hook integrity mismatch");
  } finally {
    fs.closeSync(sourceFd);
  }
  const stageRoot = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-managed-hook-"));
  const stagePath = path.join(stageRoot, "hook");
  let stagedFd = null;
  try {
    fs.chmodSync(stageRoot, 0o700);
    fs.writeFileSync(stagePath, content, { flag: "wx", mode: 0o400 });
    stagedFd = fs.openSync(stagePath, "r");
    fs.unlinkSync(stagePath);
    fs.rmdirSync(stageRoot);
    return stagedFd;
  } catch (error) {
    if (stagedFd !== null) fs.closeSync(stagedFd);
    fs.rmSync(stageRoot, { recursive: true, force: true });
    throw error;
  }
}

function spawnHook(stagedFd, scriptName, scriptPath, input, cwd) {
  return new Promise((resolve) => {
    const executable = scriptName.endsWith(".py") ? "/usr/bin/python3" : "/bin/bash";
    const args = scriptName.endsWith(".py") ? ["-I", "/dev/fd/3"] : ["/dev/fd/3"];
    const proc = spawn(executable, args, {
      cwd,
      env: { ...process.env, AGENT_FLOW_MANAGED_HOOK_SOURCE_PATH: scriptPath },
      stdio: ["pipe", "pipe", "pipe", stagedFd],
    });
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
    proc.on("error", (error) => {
      finish({
        status: 127,
        stdout: "",
        stderr: "agent-flow hook failed to start: " + String(error?.message || error),
      });
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

function installOmpHooks(root, plan) {
  if (path.resolve(root) !== plan.root) {
    throw new Error("blocked: managed OMP hook install root changed during install");
  }
  const entry = plan.entries.find(
    (candidate) => candidate.relative === ".omp/extensions/agent-flow-hooks.ts",
  );
  if (!entry) {
    throw new Error("blocked: managed OMP hook install plan is missing");
  }
  writeManagedHostFile(plan, entry);
}

function makeHooksExecutable(root, plan) {
  if (path.resolve(root) !== plan.root) {
    throw new Error("blocked: managed hook script install root changed during install");
  }
  for (const entry of plan.entries) {
    const current = inspectManagedHookScriptDestination(plan.root, entry.relative);
    if (!current.exists || current.sha256 !== entry.sha256) {
      throw new Error(`blocked: managed hook script is not authenticated: ${entry.relative}`);
    }
    if (process.platform !== "win32") {
      chmodCriticalInstallFile(entry.destination, 0o755);
    }
  }
}

function chmodCriticalInstallFile(candidate, mode) {
  const persistent = pendingProjectSkillHostTransaction?.persistent;
  if (!persistent) {
    fs.chmodSync(candidate, mode);
    return;
  }
  const temporary = path.join(
    persistent.transactionRoot,
    "writes",
    `.${path.basename(candidate)}.mode-${crypto.randomBytes(6).toString("hex")}`,
  );
  fs.copyFileSync(candidate, temporary, fs.constants.COPYFILE_EXCL);
  fs.chmodSync(temporary, mode);
  try {
    const prepared = preparePendingCriticalInstallMutation(candidate, temporary);
    assertPreparedCriticalInstallMutationUnchanged(prepared, candidate);
    fs.chmodSync(candidate, mode);
    if (process.env.AGENT_FLOW_TEST_CRASH_AFTER_HOOK_CHMOD === "1") process.exit(81);
    checkpointPendingCriticalInstall(candidate);
  } finally {
    fs.rmSync(temporary, { force: true });
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
- Required review happens before completion QA. After reviewer approve, gates run the configured profile checks. CI/CD owns lint/static/check commands; managed local profile gates keep agent-flow architecture checks and build/test checks only. If review or QA fails, fix-loop routes back through comment-authoring and review before gates run again.
- Code review requires at least two active-host sub-agents (Codex sub-agent in Codex, Claude sub-agent in Claude, OMP sub-agent in OMP). If the changed scope spans multiple areas, run one additional active-host sub-agent in parallel. Additional non-host providers are optional, and every multi-review verdict requires 2+ independent sub-agent reviewer verdicts with reviewer-source: sub-agent. After recording each sub-agent result, close that sub-agent session. End multi-review artifacts with ## Overall followed by exactly one verdict line: verdict: approve or verdict: request-changes.
- In the default workflow, gates run as their own phase after final-review approve.

Document size rules:

- \`CONTEXT.md\`, domain-grill outputs, compact domain maps, and long planning docs must stay under 200 lines each.
- If a source doc grows past 200 lines, create or refresh a matching \`*-summary.md\` under \`.Codex/rules/\` and use that summary as agent context.
- Preserve the original long doc only as reference; do not load it as hot context unless the current phase needs a specific section.
- Artifacts must link to long docs by repo-relative path and summarize only the needed decision, not paste the full content.

Context rules:

- Artifacts and manifests must use repo-relative paths; local absolute paths are forbidden.
- Do not paste full docs or raw logs into artifacts. Summarize and link by relative path.
- \`CONTEXT.md\` is hot context only and must stay under 200 lines.
- Current and future vocabulary must stay separated.
- Follow the phase context map in \`.Codex/rules/context/\` for phase-specific context loading.
- User-facing agent-flow replies must be short Korean by default. Keep code, commands, paths, and identifiers in English.
- Summarize only current phase, action, \`next_command\`, and blocker when useful.
`;
}

function runArchitectureLint(args) {
  const help = nativeGateHelpFromArgs("architecture-lint", args);
  if (help !== null) {
    process.stdout.write(help);
    process.exit(0);
  }
  const options = parseNativeCommandOptions(parseArchitectureLintArgs, args);
  const context = nativePublicCommandContext(options);
  const { profileIds, loadCanonicalProfile } = nativeProfileSelection(context, options.profile);
  const result = runNativeArchitectureLint(context.commandRoot, profileIds, {
    files: options.files,
    loadProfile: loadCanonicalProfile,
  });
  if (result.stdout) process.stdout.write(result.stdout);
  if (result.stderr) process.stderr.write(result.stderr);
  process.exit(result.passed ? 0 : 1);
}

function runGates(args) {
  const help = nativeGateHelpFromArgs("gates", args);
  if (help !== null) {
    process.stdout.write(help);
    process.exit(0);
  }
  const options = parseNativeCommandOptions(parseGatesArgs, args);
  const context = nativePublicCommandContext(options);
  const { profileIds, loadCanonicalProfile } = nativeProfileSelection(context, options.profile);
  const commands = profileGateCommands(profileIds, {
    loadProfile: loadCanonicalProfile,
    pythonExecutable: profileIds.includes("python") ? preferredPython() : "python3",
  });
  const results = runNativeGateCommands(commands, {
    cwd: context.commandRoot,
    timeoutSeconds: options.timeoutSeconds,
    profileIds,
    loadProfile: loadCanonicalProfile,
    env: nativeGateEnvironment(context),
  });
  if (options.runDir !== null) {
    const runDir = path.isAbsolute(options.runDir)
      ? path.resolve(options.runDir)
      : path.resolve(context.commandRoot, options.runDir);
    writeGateResults(runDir, results);
  }
  const summary = gateSummary(profileIds, results);
  console.log(summary.message);
  process.exit(summary.exitCode);
}

function runExperiment(args) {
  const help = experimentHelpFromArgs(args);
  if (help !== null) {
    process.stdout.write(help);
    process.exit(0);
  }
  const requestedRoot = path.resolve(process.cwd());
  const managedRoot = resolveManagedWorktreeRoot(requestedRoot);
  const configRoot = managedRoot ?? resolveAgentFlowRoot(requestedRoot);
  assertNativeCliNoOpenInstallTransaction(configRoot);
  const result = recordUsageFromArgs(args, { root: configRoot });
  if (result.stdout) process.stdout.write(result.stdout);
  if (result.stderr) process.stderr.write(result.stderr);
  process.exit(result.exitCode);
}

function parseNativeCommandOptions(parser, args) {
  try {
    const options = parser(args);
    if (options.worktree !== null) validateNativeWorktreeName(options.worktree);
    return options;
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exit(2);
  }
}

function nativePublicCommandContext(options) {
  const requestedRoot = path.resolve(process.cwd(), options.root);
  const managedRoot = resolveManagedWorktreeRoot(requestedRoot);
  const configRoot = managedRoot ?? resolveAgentFlowRoot(requestedRoot);
  assertNativeCliNoOpenInstallTransaction(configRoot);
  const snapshot = preflightNativeInstalledSnapshot(configRoot);
  const commandRoot = resolveNativeCommandRoot(
    configRoot,
    requestedRoot,
    options.worktree,
    snapshot.active?.workspace_root ?? null,
  );
  return { configRoot, commandRoot, installed: snapshot.installed };
}

function assertNativeCliNoOpenInstallTransaction(root) {
  try {
    assertNoOpenInstallTransaction(root);
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exit(2);
  }
}

function preflightNativeInstalledSnapshot(root) {
  const kitPath = path.join(root, ".agent-flow", "kit.json");
  const indexPath = path.join(root, ".agent-flow", "skills", "index.json");
  const kitPresent = lstatIfExists(kitPath) !== null;
  const indexPresent = lstatIfExists(indexPath) !== null;
  if (!kitPresent && !indexPresent) return { installed: false, active: null };
  currentSkillPlanHash(root);
  const active = findProjectActiveRun(root);
  if (active) assertSkillPlanPinned(active, root);
  return { installed: true, active };
}

function nativeProfileSelection(context, requested) {
  const loadCanonicalProfile = createCanonicalProfileLoader(path.join(KIT_ROOT, "profiles"));
  const installedSelection = context.installed ? installedProfilePromptSelection(context.configRoot) : null;
  const autoProfileIds = installedSelection?.profileIds ?? [detectProfile(context.configRoot)];
  const profileIds = requestedProfileIds(requested, {
    autoProfileIds,
    loadInstalledProfile: context.installed
      ? (profileId) => readInstalledProfilePayload(context.configRoot, profileId)
      : null,
    loadCanonicalProfile,
  });
  for (const profileId of profileIds) loadCanonicalProfile(profileId);
  return { profileIds, loadCanonicalProfile };
}

function nativeGateEnvironment(context) {
  const pythonPaths = [
    installedPythonRuntimePath(context.configRoot),
    fs.existsSync(path.join(context.commandRoot, "src")) ? path.join(context.commandRoot, "src") : "",
    fs.existsSync(path.join(KIT_ROOT, "src")) ? path.join(KIT_ROOT, "src") : "",
    process.env.PYTHONPATH,
  ].filter(Boolean);
  return {
    ...process.env,
    PYTHONPATH: [...new Set(pythonPaths)].join(path.delimiter),
  };
}

function resolveNativeCommandRoot(configRoot, requestedRoot, requestedWorktree, activeWorkspace = null) {
  const managedRoot = resolveManagedWorktreeRoot(requestedRoot);
  const managedName = managedWorktreeName(requestedRoot);
  if (managedRoot && requestedWorktree === null) return requestedRoot;
  if (managedRoot && managedName === requestedWorktree) return requestedRoot;
  if (requestedWorktree !== null) {
    validateNativeWorktreeName(requestedWorktree);
    for (const marker of [".agent-flow", ".codex", ".Codex", ".claude", ".omp"]) {
      const literal = path.join(configRoot, marker, "worktrees", requestedWorktree);
      if (lstatIfExists(path.join(literal, ".git"))) return literal;
    }
    const identity = explicitWorkspaceIdentity(
      configRoot,
      requestedWorktree,
      null,
      configuredWorktreeNaming(configRoot),
    );
    const registered = matchingRegisteredWorkspace(configRoot, identity);
    if (registered) return registered.path;
    throw new Error(`worktree not found or missing path: ${requestedWorktree}`);
  }
  const currentDirectory = path.resolve(process.cwd());
  const currentManagedRoot = resolveManagedWorktreeRoot(currentDirectory);
  if (
    currentManagedRoot
    && samePath(currentManagedRoot, configRoot)
    && (samePath(requestedRoot, configRoot) || samePath(requestedRoot, currentDirectory))
  ) {
    return resolveGitTopLevel(currentDirectory) ?? currentDirectory;
  }
  if (
    typeof activeWorkspace === "string"
    && activeWorkspace
    && registeredManagedWorktree(configRoot, path.resolve(activeWorkspace))
  ) {
    return path.resolve(activeWorkspace);
  }
  const gitRoot = resolveGitTopLevel(requestedRoot);
  if (gitRoot && !samePath(gitRoot, configRoot) && registeredManagedWorktree(configRoot, gitRoot)) {
    return gitRoot;
  }
  return configRoot;
}

function managedWorktreeName(candidate) {
  const parts = path.resolve(candidate).split(path.sep);
  const markers = new Set([".agent-flow", ".codex", ".Codex", ".claude", ".omp"]);
  for (let index = parts.length - 2; index >= 0; index -= 1) {
    if (markers.has(parts[index]) && parts[index + 1] === "worktrees") return parts[index + 2] ?? null;
  }
  return null;
}

function validateNativeWorktreeName(value) {
  if (
    typeof value !== "string"
    || !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(value)
    || value === "."
    || value === ".."
  ) {
    throw new Error(`invalid worktree name: ${JSON.stringify(value)}`);
  }
}

function installedPythonRuntimePath(root) {
  const runtimePath = path.join(root, RUNTIME_PYTHON_RELATIVE);
  return fs.existsSync(path.join(runtimePath, "agent_flow", "__init__.py")) ? runtimePath : "";
}

function isPinnedNodeRuntime() {
  return path.basename(KIT_ROOT) === "node"
    && path.basename(path.dirname(KIT_ROOT)) === "runtime"
    && path.basename(path.dirname(path.dirname(KIT_ROOT))) === ".agent-flow";
}

try {
  if (command === "install") {
    if (isPinnedNodeRuntime()) {
      throw new Error("blocked: the pinned project runtime cannot install; run the package installer from the leader checkout");
    }
    installProject();
    process.exit(0);
  }

  if (command === "run" && process.argv[3] === "install") {
    if (isPinnedNodeRuntime()) {
      throw new Error("blocked: the pinned project runtime cannot install; run the package installer from the leader checkout");
    }
    installProject();
    process.exit(0);
  }

  if (command === "run") {
    runWorkflowCommand(process.argv.slice(3));
    process.exit(0);
  }

  if (command === "status") {
    runWorkflowCommand(["status", ...process.argv.slice(3)]);
    process.exit(0);
  }

  if (command === "architecture-lint") {
    runArchitectureLint(process.argv.slice(3));
  }

  if (command === "gates") {
    runGates(process.argv.slice(3));
  }

  if (command === "experiment") {
    runExperiment(process.argv.slice(3));
  }

  console.error("usage: agent-flow install | status | run <task> | run <start|status|next|advance|push-watch|push-watch-tick> | experiment record-usage");
  process.exit(1);
} catch (error) {
  const rollbackErrors = rollbackPendingProjectSkillHostTransaction();
  const message = error instanceof Error ? error.message : String(error);
  console.error(rollbackErrors.length > 0 ? `${message}; ${rollbackErrors.join("; ")}` : message);
  process.exit(1);
}
