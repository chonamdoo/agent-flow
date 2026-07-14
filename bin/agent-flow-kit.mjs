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
  hashSkillTree,
  mergeInstallSelectionWithPrevious,
  mergeResolvedSkillClosure,
  resolveInstallSelection,
  resolveProfileSkillSources,
} from "../lib/skill-selection.mjs";
import { detectActiveHost } from "../lib/host-detection.mjs";

const command = process.argv[2];
const AGENT_FLOW_COMMAND = "agent-flow";
const HOME = process.env.HOME || process.env.USERPROFILE || "";
const KIT_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const RUNTIME_PYTHON_RELATIVE = path.join(".agent-flow", "runtime", "python");
const installArgs = process.argv.slice(3);
const forceManaged = installArgs.includes("--force-managed");
let cachedFullFeatureWorkflow = null;
let activeManagedInstallTransaction = null;
const PROJECT_SKILL_HOSTS = Object.freeze(["claude", "codex", "omp"]);
const SKILL_LINKS_COMMITMENT_VERSION = 1;
const MANAGED_HOST_FILES_VERSION = 1;
const MANAGED_HOST_FILES_COMMITMENT_VERSION = 1;
const MANAGED_HOOK_CONTRACT_VERSION = 2;
const MANAGED_HOOK_CONTRACT_COMMITMENT_VERSION = 2;
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
const WRITE_TOOL_MATCHER = "^(apply_patch|Write|Edit|MultiEdit|NotebookEdit|Eval|Python|Notebook|write|edit|multi_edit|multiedit|notebook_edit|notebookedit|eval|python|notebook)$";
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
function installProject(rootOverride = null) {
  const requestedRoot = rootOverride ? path.resolve(rootOverride) : process.cwd();
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
  fs.mkdirSync(agentFlowDir, { recursive: true });
  const lock = acquireProjectInstallLock(root, agentFlowDir);
  const context = { transaction: null };
  try {
    recoverInterruptedSkillTransaction(root, agentFlowDir, lock.token);
    installProjectUnlocked(root, context);
    commitSkillInstallTransaction(context.transaction);
    try {
      installCodexTrustState(root);
    } catch (error) {
      console.error(`warning: Codex trust registration failed after install commit: ${error.message}`);
    }
  } catch (error) {
    let rollbackError = null;
    try {
      sealManagedInstallMutations(context.transaction);
      rollbackSkillInstallTransaction(context.transaction);
    } catch (failure) {
      rollbackError = failure;
    }
    if (rollbackError) {
      throw new Error(`${error instanceof Error ? error.message : String(error)}; rollback failed: ${rollbackError.message}`);
    }
    throw error;
  } finally {
    releaseProjectInstallLock(lock);
  }
}

function syncProject(rootOverride = null) {
  const requestedRoot = rootOverride ? path.resolve(rootOverride) : process.cwd();
  if (resolveManagedWorktreeRoot(requestedRoot)) {
    throw new Error("managed worktree sync blocked; run sync from the leader checkout");
  }
  const root = resolveInstallRoot(requestedRoot);
  syncProjectAgentDocuments(root);
  console.log(`agent-flow documents synced root=${root}`);
}

function installProjectUnlocked(root, context) {
  const agentFlowDir = path.join(root, ".agent-flow");
  const profile = detectProfile(root);
  let installSelection = resolveInstallSelection({ args: installArgs, detectedProfile: profile, kitRoot: KIT_ROOT, projectRoot: root });
  const existingPayload = readExistingKit(agentFlowDir);
  const previousIndexRecord = readAuthenticatedSkillIndex(agentFlowDir, existingPayload);
  holdInstallForTest("AGENT_FLOW_TEST_HOLD_AFTER_INDEX_AUTH_MS", "index-authenticated");
  const previousSkillIndex = previousIndexRecord?.payload || null;
  installSelection = mergeInstallSelectionWithPrevious(installSelection, previousSkillIndex, KIT_ROOT, root);
  const activeHost = detectActiveHost(process.env);
  const automaticSkillNames = discoverAutomaticExternalSkillNames({
    home: HOME,
    activeHost,
    env: process.env,
    previousIndex: previousSkillIndex,
  });
  const sourceNames = new Set([
    ...automaticSkillNames,
    ...(installSelection.explicitSkills || []),
  ]);
  const skillSourcePlan = resolveProfileSkillSources({
    skillNames: sourceNames,
    kitRoot: KIT_ROOT,
    projectRoot: root,
    projectSkillsRoot: path.join(agentFlowDir, "skills"),
    home: HOME,
    activeHost,
    env: process.env,
    previousIndex: previousSkillIndex,
    automaticSkillNames,
    explicitSkillNames: installSelection.explicitSkills,
  });
  for (const name of installSelection.explicitSkills || []) {
    if (skillSourcePlan.missing.includes(name)) throw new Error(`explicit skill not found: ${name}`);
  }
  installSelection = mergeResolvedSkillClosure(installSelection, skillSourcePlan);
  context.transaction = beginSkillInstallTransaction(root, agentFlowDir, previousIndexRecord);
  const phases = fullFeaturePhases();

  for (const name of ["runs", "state", "handoffs", "team", "worktrees", "skills"]) {
    fs.mkdirSync(path.join(agentFlowDir, name), { recursive: true });
  }
  fs.mkdirSync(path.join(agentFlowDir, "local-skills"), { recursive: true });

  fs.mkdirSync(path.join(agentFlowDir, "skills", "agent-flow"), { recursive: true });
  fs.mkdirSync(path.join(agentFlowDir, "skills", "full-feature-workflow"), { recursive: true });

  const payload = {
    install_scope: "project",
    profile,
    profiles: installSelection.profiles,
    profile_selection: installSelection.profileSelection || "auto",
    selected_skills: installSelection.skillNames ? [...installSelection.skillNames].sort() : "all",
    root: ".",
    installed_at: existingPayload?.installed_at || new Date().toISOString(),
  };

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
    new Set(["index.json", ".agent-flow-transaction-owner", ...GENERATED_PROJECT_SKILL_NAMES]),
    installSelection.copyRootNames,
  );
  materializeResolvedSkillSources(agentFlowDir, skillSourcePlan, context.transaction);
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, "profiles"), path.join(agentFlowDir, "profiles"), forceManaged);
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, "templates"), path.join(agentFlowDir, "templates"), forceManaged, new Set(), true, forceManaged);
  const skillIndex = installProjectSkills(
    root,
    agentFlowDir,
    previousSkillIndex,
    forceManaged,
    installSelection,
    skillSourcePlan,
    context.transaction,
  );
  preserveUnmanagedSkillEntries(context.transaction, previousSkillIndex, skillIndex);
  if (process.env.AGENT_FLOW_TEST_CRASH_AFTER_SKILL_INDEX === "1") {
    sealManagedInstallMutations(context.transaction);
    process.exit(87);
  }
  if (process.env.AGENT_FLOW_TEST_FAIL_AFTER_SKILL_INDEX === "1") {
    throw new Error("injected failure after skill index");
  }
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, "scripts"), path.join(agentFlowDir, "scripts"), forceManaged);
  removeStaleContextDocsScripts(agentFlowDir, forceManaged);
  copyBundledDirIfMissingOrSame(
    path.join(KIT_ROOT, "src", "agent_flow"),
    path.join(root, RUNTIME_PYTHON_RELATIVE, "agent_flow"),
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
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, ".claude", "agents"), path.join(root, ".omp", "agents"), forceManaged);
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
  for (const phase of phases) {
    writeManagedFile(
      path.join(agentFlowDir, "prompts", `${phase.id}.md`),
      phasePrompt(phase, root),
    );
  }
  writeManagedFile(path.join(agentFlowDir, "rules", "workflow-contract.md"), workflowContract());
  const agentFlowBlock = canonicalAgentFlowBlock();
  writeManagedFile(path.join(agentFlowDir, "bootstrap", "agent-flow.md"), agentFlowBlock);
  writeManagedFile(path.join(agentFlowDir, "bootstrap", "AGENTS.md"), bootstrapMarkdown("AGENTS.md", agentFlowBlock));
  writeManagedFile(path.join(agentFlowDir, "bootstrap", "CLAUDE.md"), bootstrapMarkdown("CLAUDE.md", agentFlowBlock));
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
    "scripts/check-context-docs.*",
    "graphify/",
    "graphify-out/manifest.json",
    "graphify-out/cost.json",
  ]);
  if (!skillIndex.skills.some((skill) => skill.name === "graphify")) {
    removeLegacyProjectSkillCopies(root, "graphify");
  }
  syncProjectAgentDocuments(root, agentFlowBlock);
  makeHooksExecutable(root);
  installClaudeHooks(root);
  installOmpHooks(root);

  payload.skill_index = {
    path: ".agent-flow/skills/index.json",
    skills: skillIndex.skills.length,
    conflicts: skillIndex.conflicts.length,
    warnings: skillIndex.warnings.length,
  };

  const indexBytes = fs.readFileSync(path.join(agentFlowDir, "skills", "index.json"));
  payload.skill_index_hash_version = 1;
  payload.skill_index_hash = crypto.createHash("sha256").update(indexBytes).digest("hex");
  payload.skill_plan_hash_version = 2;
  payload.skill_plan_hash = computeSkillPlanHash(skillIndex, root, true);
  payload.skill_links_commitment_version = SKILL_LINKS_COMMITMENT_VERSION;
  payload.skill_links_commitment = skillLinksCommitment(payload.skill_plan_hash, skillIndex.links);
  payload.managed_host_files = managedHostFileManifest(root);
  payload.managed_host_files_commitment_version = MANAGED_HOST_FILES_COMMITMENT_VERSION;
  payload.managed_host_files_commitment = managedHostFilesCommitment(payload);
  payload.managed_hook_contract = managedHookContract(root);
  payload.managed_hook_contract_commitment_version = MANAGED_HOOK_CONTRACT_COMMITMENT_VERSION;
  payload.managed_hook_contract_commitment = managedHookContractCommitment(payload);

  writeManagedFile(path.join(agentFlowDir, "kit.json"), `${JSON.stringify(payload, null, 2)}\n`);
  sealManagedInstallMutations(context.transaction);
  holdInstallForTest("AGENT_FLOW_TEST_HOLD_AFTER_MANAGED_INSTALL_SEAL_MS", "managed-install-sealed");
  if (process.env.AGENT_FLOW_TEST_FAIL_AFTER_MANAGED_INSTALL === "1") {
    throw new Error("injected failure after managed install");
  }
  console.log(`agent-flow installed profile=${profile}`);
}

function runWorkflowCommand(args) {
  const subcommand = args[0];
  const root = resolveAgentFlowRoot(process.cwd());
  refreshSkillCatalogAtBoundary(root);
  if (subcommand === "start") {
    const task = optionValue(args, "--task");
    if (!task) {
      throw new Error("run start requires --task");
    }
    const workflow = optionValue(args, "--workflow") ?? "full-feature";
    const runId = optionValue(args, "--run-id") ?? newRunId();
    assertInstalled(root);
    const phases = workflowPhases(workflow);
    const runDir = path.join(root, ".agent-flow", "runs", workflow, runId);
    const runDirRel = path.join(".agent-flow", "runs", workflow, runId);
    if (fs.existsSync(runDir)) {
      throw new Error(`run already exists: ${runId}`);
    }
    const workspace = captureNodeWorkspaceIdentity(process.cwd(), root);
    const skillPlan = currentNodeSkillPlan(root);
    fs.mkdirSync(path.join(runDir, "artifacts"), { recursive: true });
    fs.mkdirSync(path.join(runDir, "logs"), { recursive: true });
    const startedAt = new Date().toISOString();
    const state = {
      run_id: runId,
      workflow,
      task,
      phase_index: 0,
      phase: phases[0].id,
      status: "running",
      run_dir: runDirRel,
      started_at: startedAt,
      phase_entered_at: startedAt,
      workspace_root: workspace.workspace_root,
      ...(workspace.identity ? { workspace: workspace.identity } : {}),
      ...skillPlan,
    };
    writeJson(path.join(runDir, "manifest.json"), state);
    writeJson(currentRunPath(root), state);
    printNext(state, root);
    return;
  }

  if (subcommand === "status") {
    const state = assertNodeRunBoundary(readCurrentRun(root), root);
    assertNodeSkillPlanPinned(state, root);
    printStatus(state, root);
    return;
  }

  if (subcommand === "next") {
    const state = assertNodeRunBoundary(readCurrentRun(root), root);
    assertNodeSkillPlanPinned(state, root);
    printNext(state, root);
    return;
  }

  if (subcommand === "push-watch") {
    assertInstalled(root);
    let current;
    try {
      current = readCurrentRun(root);
    } catch (error) {
      const invocationBranch = currentBranch(process.cwd());
      if (["main", "master", "develop"].includes(invocationBranch)) {
        throw new Error(`blocked: protected branch ${invocationBranch}`);
      }
      throw error;
    }
    const active = assertNodeRunBoundary(current, root);
    assertNodeSkillPlanPinned(active, root);
    const branch = currentBranch(active.workspace_root ?? process.cwd());
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
    const state = assertNodeRunBoundary(readCurrentRun(root), root);
    assertNodeSkillPlanPinned(state, root);
    if (state.phase !== "pr-watch") {
      throw new Error(`blocked: push-watch-tick requires current phase pr-watch, got ${state.phase}`);
    }
    const runDir = resolveRunDir(root, state.run_dir);
    const pr = readPullRequestStatus(state.workspace_root ?? process.cwd());
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

  if (subcommand === "advance") {
    const state = assertNodeRunBoundary(readCurrentRun(root), root);
    assertNodeSkillPlanPinned(state, root);
    const runDir = resolveRunDir(root, state.run_dir);
    if (state.status === "complete" || state.phase === "complete") {
      console.log(`workflow already complete: ${state.run_id}`);
      return;
    }
    const phases = workflowPhases(state.workflow);
    const phase = phases[state.phase_index];
    const artifact = path.join(runDir, phase.artifact);
    if (!fs.existsSync(artifact)) {
      throw new Error(`blocked: missing artifact ${artifact}`);
    }
    assertFreshArtifact(state, phase, artifact);
    assertCompletionMarkers(phase, artifact, root);
    const nextIndex = nextPhaseIndex(state, phases, phase, artifact);
    syncRouteArtifacts(runDir, phases, state.phase_index, nextIndex);
    const nextPhase = phases[nextIndex];
    const transitionedAt = new Date().toISOString();
    const fixLoopRounds = nextFixLoopRounds(state, phase, nextPhase);
    const nextState = {
      ...state,
      phase_index: nextIndex,
      phase: nextPhase?.id ?? "complete",
      status: nextPhase ? "running" : "complete",
      updated_at: transitionedAt,
      phase_entered_at: transitionedAt,
      fix_loop_rounds: fixLoopRounds,
    };
    writeJson(path.join(runDir, "manifest.json"), nextState);
    writeJson(currentRunPath(root), nextState);
    if (nextPhase) {
      printNext(nextState, root);
    } else {
      console.log(`workflow complete: ${state.run_id}`);
    }
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
  const markers = new Set([".agent-flow", ".codex", ".Codex", ".omp"]);
  for (let index = parts.length - 2; index >= 0; index -= 1) {
    if (parts[index + 1] !== "worktrees") continue;
    if (!markers.has(parts[index])) continue;
    const root = parts.slice(0, index).join(path.sep) || path.sep;
    // 홈의 전역 Codex/OMP worktree는 프로젝트 내부 worktree가 아니다.
    if (HOME && samePath(root, HOME) && (parts[index] === ".codex" || parts[index] === ".Codex" || parts[index] === ".omp")) {
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

function captureNodeWorkspaceIdentity(cwd, root) {
  const topLevel = gitOutput(cwd, ["rev-parse", "--show-toplevel"]);
  if (!topLevel) {
    return { workspace_root: path.resolve(root), identity: null };
  }
  const workspaceRoot = fs.realpathSync(topLevel);
  const commonDir = gitOutput(workspaceRoot, ["rev-parse", "--path-format=absolute", "--git-common-dir"]);
  const gitDir = gitOutput(workspaceRoot, ["rev-parse", "--path-format=absolute", "--git-dir"]);
  const branch = gitOutput(workspaceRoot, ["branch", "--show-current"]);
  const head = gitOutput(workspaceRoot, ["rev-parse", "HEAD"]);
  if (!commonDir || !gitDir || !branch || !head) {
    throw new Error(`blocked: cannot capture pinned workspace identity: ${workspaceRoot}`);
  }
  const canonicalCommon = fs.realpathSync(commonDir);
  if (path.basename(canonicalCommon) !== ".git" || !samePath(path.dirname(canonicalCommon), root)) {
    throw new Error(`blocked: pinned workspace belongs to a different repository: ${workspaceRoot}`);
  }
  if (["main", "master", "develop"].includes(branch)) {
    throw new Error(`blocked: pinned workspace uses protected branch ${branch}`);
  }
  const metadata = fs.statSync(workspaceRoot);
  const identity = {
    workspace_root: workspaceRoot,
    git_common_dir: canonicalCommon,
    git_dir: fs.realpathSync(gitDir),
    branch,
    head,
    device: metadata.dev,
    inode: metadata.ino,
  };
  if (!samePath(workspaceRoot, root) && !registeredNodeWorkspaceIdentity(root, workspaceRoot)) {
    registerNodeWorkspaceIdentity(root, identity);
  }
  validateNodeWorkspaceIdentity(identity, root);
  return { workspace_root: workspaceRoot, identity };
}

function validateNodeWorkspaceIdentity(identity, root, requireRegistration = true) {
  if (!identity || typeof identity !== "object") {
    throw new Error("blocked: pinned workspace identity is missing");
  }
  const configured = path.resolve(String(identity.workspace_root ?? ""));
  let workspaceRoot;
  try {
    workspaceRoot = fs.realpathSync(configured);
  } catch {
    throw new Error(`blocked: pinned workspace is missing: ${configured}`);
  }
  if (workspaceRoot !== String(identity.workspace_root)) {
    throw new Error(`blocked: pinned workspace canonical path changed: ${configured}`);
  }
  const metadata = fs.statSync(workspaceRoot);
  if (metadata.dev !== identity.device || metadata.ino !== identity.inode) {
    throw new Error(`blocked: pinned workspace filesystem identity changed: ${workspaceRoot}`);
  }
  const commonDir = gitOutput(workspaceRoot, ["rev-parse", "--path-format=absolute", "--git-common-dir"]);
  const gitDir = gitOutput(workspaceRoot, ["rev-parse", "--path-format=absolute", "--git-dir"]);
  const branch = gitOutput(workspaceRoot, ["branch", "--show-current"]);
  const head = gitOutput(workspaceRoot, ["rev-parse", "HEAD"]);
  if (
    !commonDir
    || !gitDir
    || !head
    || !samePath(commonDir, identity.git_common_dir)
    || !samePath(gitDir, identity.git_dir)
  ) {
    throw new Error(`blocked: pinned workspace git identity changed: ${workspaceRoot}`);
  }
  if (branch !== identity.branch) {
    throw new Error(`blocked: pinned workspace branch changed: ${workspaceRoot}`);
  }
  const ancestor = safeSpawnSync(
    "git",
    ["merge-base", "--is-ancestor", String(identity.head), head],
    { cwd: workspaceRoot, stdio: "ignore" },
  );
  if (ancestor.error || ancestor.status !== 0) {
    throw new Error(`blocked: pinned workspace HEAD diverged: ${workspaceRoot}`);
  }
  const canonicalCommon = fs.realpathSync(commonDir);
  if (path.basename(canonicalCommon) !== ".git" || !samePath(path.dirname(canonicalCommon), root)) {
    throw new Error(`blocked: pinned workspace repository identity changed: ${workspaceRoot}`);
  }
  if (!samePath(workspaceRoot, root) && requireRegistration) {
    const registered = registeredNodeWorkspaceIdentity(root, workspaceRoot);
    if (!registered) {
      throw new Error(`blocked: pinned workspace is not registered: ${workspaceRoot}`);
    }
    validateNodeWorkspaceIdentity(registered, root, false);
  }
  return workspaceRoot;
}

function registeredNodeWorkspaceIdentity(root, workspaceRoot) {
  const commonDir = gitOutput(root, ["rev-parse", "--path-format=absolute", "--git-common-dir"]);
  if (!commonDir) return null;
  const registrations = path.join(commonDir, "agent-flow", "worktrees");
  if (!fs.existsSync(registrations)) return null;
  for (const entry of fs.readdirSync(registrations, { withFileTypes: true })) {
    if (!entry.isDirectory() || entry.isSymbolicLink()) continue;
    const manifest = readJsonIfExists(path.join(registrations, entry.name, "manifest.json"));
    const identity = manifest?.identity;
    if (identity?.workspace_root && samePath(identity.workspace_root, workspaceRoot)) {
      return identity;
    }
  }
  return null;
}

function registerNodeWorkspaceIdentity(root, identity) {
  const commonDir = gitOutput(root, ["rev-parse", "--path-format=absolute", "--git-common-dir"]);
  if (!commonDir) {
    throw new Error("blocked: cannot register pinned workspace without a git common directory");
  }
  const digest = crypto.createHash("sha256").update(identity.workspace_root).digest("hex").slice(0, 12);
  const name = `node-${digest}`;
  const runtime = path.join(commonDir, "agent-flow", "worktrees", name);
  const manifestPath = path.join(runtime, "manifest.json");
  const existing = readJsonIfExists(manifestPath);
  if (existing?.identity?.workspace_root && !samePath(existing.identity.workspace_root, identity.workspace_root)) {
    throw new Error(`blocked: pinned workspace registration collision: ${identity.workspace_root}`);
  }
  writeJson(manifestPath, {
    name,
    branch: identity.branch,
    path: identity.workspace_root,
    identity,
  });
}

function assertNodeRunBoundary(state, root) {
  const workspaceRoot = path.resolve(state.workspace_root ?? root);
  if (state.workspace) {
    const pinned = validateNodeWorkspaceIdentity(state.workspace, root);
    if (!samePath(pinned, workspaceRoot)) {
      throw new Error("blocked: run workspace_root differs from its pinned identity");
    }
    const invocation = gitOutput(process.cwd(), ["rev-parse", "--show-toplevel"])
      ?? path.resolve(process.cwd());
    if (!samePath(invocation, root) && !samePath(invocation, pinned)) {
      throw new Error(
        `blocked: active run ${state.run_id} is pinned to ${pinned}; current workspace is ${invocation}`,
      );
    }
    return state;
  }
  if (gitOutput(root, ["rev-parse", "--show-toplevel"])) {
    throw new Error("blocked: active run is missing its pinned workspace identity");
  }
  return state;
}

function managedHostSourceSpecs(root) {
  return [
    [".Codex/agents/code-reviewer.md", ".Codex/agents/code-reviewer.md", path.join(KIT_ROOT, ".Codex", "agents", "code-reviewer.md")],
    [".claude/agents/code-reviewer.md", ".claude/agents/code-reviewer.md", path.join(KIT_ROOT, ".claude", "agents", "code-reviewer.md")],
    [".omp/agents/code-reviewer.md", ".claude/agents/code-reviewer.md", path.join(KIT_ROOT, ".claude", "agents", "code-reviewer.md")],
    [".omp/extensions/agent-flow-hooks.ts", "generated:omp-hooks-extension", Buffer.from(ompHooksExtensionSource(), "utf8")],
  ].map(([relative, source, sourceValue]) => ({
    relative,
    source,
    sourceBytes: Buffer.isBuffer(sourceValue) ? sourceValue : fs.readFileSync(sourceValue),
    destination: path.join(root, ...relative.split("/")),
  }));
}

function requireManagedRegularFile(root, relative) {
  let cursor = path.resolve(root);
  for (const [index, part] of relative.split("/").entries()) {
    cursor = path.join(cursor, part);
    const metadata = lstatIfExists(cursor);
    if (!metadata || metadata.isSymbolicLink()) {
      throw new Error(`blocked: managed host file is missing or unsafe: ${relative}`);
    }
    const final = index === relative.split("/").length - 1;
    if ((final && !metadata.isFile()) || (!final && !metadata.isDirectory())) {
      throw new Error(`blocked: managed host file has an invalid path: ${relative}`);
    }
    if (final && metadata.nlink !== 1) {
      throw new Error(`blocked: managed host file may not be hard-linked: ${relative}`);
    }
  }
  ensureChildPath(root, cursor);
  return fs.readFileSync(cursor);
}

function managedReviewerBody(content) {
  const text = content.toString("utf8");
  if (!text.startsWith("---\n")) return text;
  const end = text.indexOf("\n---\n", 4);
  return end === -1 ? text : text.slice(end + 5).replace(/^\n/, "");
}

function managedHostFileManifest(root) {
  const specs = managedHostSourceSpecs(root);
  const codex = specs.find((spec) => spec.relative.startsWith(".Codex/"));
  const claude = specs.find((spec) => spec.relative.startsWith(".claude/"));
  const omp = specs.find((spec) => spec.relative.startsWith(".omp/agents/"));
  if (
    !codex
    || !claude
    || !omp
    || managedReviewerBody(codex.sourceBytes) !== managedReviewerBody(claude.sourceBytes)
    || !omp.sourceBytes.equals(claude.sourceBytes)
  ) {
    throw new Error("blocked: Claude, Codex, and OMP managed reviewers are not equivalent");
  }
  const files = {};
  for (const spec of specs.sort((left, right) => compareCodePoints(left.relative, right.relative))) {
    const installed = requireManagedRegularFile(root, spec.relative);
    if (!installed.equals(spec.sourceBytes)) {
      throw new Error(`blocked: managed host file differs from authenticated source: ${spec.relative}`);
    }
    files[spec.relative] = { source: spec.source, sha256: sha256Bytes(installed) };
  }
  return { version: MANAGED_HOST_FILES_VERSION, files };
}

function normalizedManagedHostFiles(manifest) {
  if (
    !manifest
    || typeof manifest !== "object"
    || Array.isArray(manifest)
    || manifest.version !== MANAGED_HOST_FILES_VERSION
    || !manifest.files
    || typeof manifest.files !== "object"
    || Array.isArray(manifest.files)
  ) throw new Error("blocked: installed managed host file provenance is invalid");
  const rows = [];
  for (const relative of Object.keys(manifest.files).sort(compareCodePoints)) {
    const entry = manifest.files[relative];
    if (
      !REQUIRED_MANAGED_HOST_FILES.includes(relative)
      || !entry
      || typeof entry.source !== "string"
      || !entry.source.trim()
      || typeof entry.sha256 !== "string"
      || !/^[0-9a-f]{64}$/.test(entry.sha256)
    ) throw new Error(`blocked: installed managed host file provenance is invalid: ${relative}`);
    rows.push([relative, entry.source, entry.sha256]);
  }
  for (const relative of REQUIRED_MANAGED_HOST_FILES) {
    if (!manifest.files[relative]) {
      throw new Error(`blocked: installed managed host file provenance is missing: ${relative}`);
    }
  }
  return rows;
}

function managedHostFilesCommitment(payload) {
  const body = {
    version: MANAGED_HOST_FILES_COMMITMENT_VERSION,
    skill_plan_hash: payload.skill_plan_hash,
    files: normalizedManagedHostFiles(payload.managed_host_files),
  };
  return sha256Bytes(Buffer.from(JSON.stringify(body), "utf8"));
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

function managedHookProjection(root, settings, label, expectedScriptHashes) {
  if (!settings?.hooks || typeof settings.hooks !== "object" || Array.isArray(settings.hooks)) {
    throw new Error(`blocked: managed hook settings are missing: ${label}`);
  }
  const rows = [];
  for (const [event, entries] of Object.entries(settings.hooks)) {
    if (!Array.isArray(entries)) throw new Error(`blocked: invalid managed hook settings: ${label}`);
    for (const entry of entries) {
      if (!entry || !Array.isArray(entry.hooks)) throw new Error(`blocked: invalid managed hook settings: ${label}`);
      const matcher = typeof entry.matcher === "string" ? entry.matcher : "";
      for (const hook of entry.hooks) {
        const scriptName = trustedManagedHookScriptName(root, hook?.command, expectedScriptHashes);
        if (scriptName) rows.push([event, matcher, hook.type ?? "", scriptName]);
        else if (managedHookScriptName(hook?.command)) {
          throw new Error(`blocked: managed hook command is not immutable: ${label}`);
        }
      }
    }
  }
  return rows.sort(compareHookProjectionRows);
}

function managedHookContract(root) {
  const scripts = {};
  for (const scriptName of MANAGED_HOOK_SCRIPT_NAMES) {
    const relative = `.agent-flow/scripts/hooks/${scriptName}`;
    const content = requireManagedRegularFile(root, relative);
    const source = fs.readFileSync(path.join(KIT_ROOT, "scripts", "hooks", scriptName));
    if (!content.equals(source)) throw new Error(`blocked: managed hook script differs from authenticated source: ${relative}`);
    if (process.platform !== "win32" && !(fs.statSync(path.join(root, ...relative.split("/"))).mode & 0o111)) {
      throw new Error(`blocked: managed hook script is not executable: ${relative}`);
    }
    scripts[relative] = { sha256: sha256Bytes(content), mode: "executable" };
  }
  const scriptHashes = new Map(Object.entries(scripts).map(([relative, entry]) => [relative, entry.sha256]));
  const expected = expectedManagedHookProjection();
  const configs = {};
  for (const relative of MANAGED_HOOK_CONFIG_PATHS) {
    let settings;
    try {
      settings = JSON.parse(requireManagedRegularFile(root, relative).toString("utf8"));
    } catch (error) {
      throw new Error(`blocked: managed hook settings are unreadable: ${relative}: ${error.message}`);
    }
    const projection = managedHookProjection(root, settings, relative, scriptHashes);
    if (JSON.stringify(projection) !== JSON.stringify(expected)) {
      throw new Error(
        `blocked: managed hook settings do not match required contract: ${relative}; `
        + `actual=${JSON.stringify(projection)} expected=${JSON.stringify(expected)}`,
      );
    }
    configs[relative] = { sha256: sha256Bytes(Buffer.from(JSON.stringify(projection), "utf8")) };
  }
  return { version: MANAGED_HOOK_CONTRACT_VERSION, configs, scripts };
}

function normalizedManagedHookContract(contract) {
  if (!contract || contract.version !== MANAGED_HOOK_CONTRACT_VERSION) {
    throw new Error("blocked: installed managed hook contract is invalid");
  }
  const normalize = (entries, expectedPaths, label, requiredMode = null) => {
    if (!entries || typeof entries !== "object" || Array.isArray(entries)) {
      throw new Error(`blocked: installed managed hook ${label} provenance is invalid`);
    }
    const actual = Object.keys(entries).sort(compareCodePoints);
    const expected = [...expectedPaths].sort(compareCodePoints);
    if (JSON.stringify(actual) !== JSON.stringify(expected)) {
      throw new Error(`blocked: installed managed hook ${label} provenance is incomplete`);
    }
    return actual.map((relative) => {
      const entry = entries[relative];
      if (!entry || !/^[0-9a-f]{64}$/.test(entry.sha256) || (requiredMode && entry.mode !== requiredMode)) {
        throw new Error(`blocked: installed managed hook ${label} provenance is invalid: ${relative}`);
      }
      return requiredMode ? [relative, entry.sha256, requiredMode] : [relative, entry.sha256];
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

function managedHookContractCommitment(payload) {
  const normalized = normalizedManagedHookContract(payload.managed_hook_contract);
  return sha256Bytes(Buffer.from(JSON.stringify({
    version: MANAGED_HOOK_CONTRACT_COMMITMENT_VERSION,
    skill_plan_hash: payload.skill_plan_hash,
    configs: normalized.configs,
    scripts: normalized.scripts,
  }), "utf8"));
}

function assertManagedHostFilesInstalled(root, kit) {
  if (
    kit.managed_host_files_commitment_version !== MANAGED_HOST_FILES_COMMITMENT_VERSION
    || kit.managed_host_files_commitment !== managedHostFilesCommitment(kit)
  ) throw new Error("blocked: installed managed host file commitment is invalid");
  for (const [relative, _source, expectedHash] of normalizedManagedHostFiles(kit.managed_host_files)) {
    if (sha256Bytes(requireManagedRegularFile(root, relative)) !== expectedHash) {
      throw new Error(`blocked: installed managed host file changed: ${relative}`);
    }
  }
  if (
    kit.managed_hook_contract_commitment_version !== MANAGED_HOOK_CONTRACT_COMMITMENT_VERSION
    || kit.managed_hook_contract_commitment !== managedHookContractCommitment(kit)
  ) throw new Error("blocked: installed managed hook commitment is invalid");
  const live = managedHookContract(root);
  if (JSON.stringify(live) !== JSON.stringify(kit.managed_hook_contract)) {
    throw new Error("blocked: installed managed hook contract changed");
  }
}

function currentNodeSkillPlan(root) {
  const agentFlowDir = path.join(root, ".agent-flow");
  const kit = readExistingKit(agentFlowDir);
  const authenticated = readAuthenticatedSkillIndex(agentFlowDir, kit);
  if (
    !authenticated
    || kit?.skill_plan_hash_version !== 2
    || typeof kit.skill_plan_hash !== "string"
  ) {
    throw new Error("blocked: installed skill plan commitment is missing");
  }
  if (computeSkillPlanHash(authenticated.payload, root, true) !== kit.skill_plan_hash) {
    throw new Error("blocked: installed skill snapshot no longer matches kit commitment");
  }
  assertManagedHostFilesInstalled(root, kit);
  return {
    skill_plan_hash_version: 2,
    skill_plan_hash: kit.skill_plan_hash,
  };
}

function assertNodeSkillPlanPinned(state, root) {
  const current = currentNodeSkillPlan(root);
  if (
    state.skill_plan_hash_version !== current.skill_plan_hash_version
    || state.skill_plan_hash !== current.skill_plan_hash
  ) {
    const previousHash = state.skill_plan_hash ?? null;
    Object.assign(state, current, {
      skill_plan_repin_at: new Date().toISOString(),
      skill_plan_repin_from: previousHash,
    });
    writeJson(path.join(resolveRunDir(root, state.run_dir), "manifest.json"), state);
    writeJson(currentRunPath(root), state);
  }
}

function safeSpawnSync(commandName, args, options = {}) {
  // 외부 CLI는 자동 relay를 멈추지 않도록 기본 timeout을 둔다.
  return spawnSync(commandName, args, {
    timeout: options.timeout ?? 30_000,
    ...options,
  });
}

function readCurrentRun(root) {
  const pathName = currentRunPath(root);
  if (!fs.existsSync(pathName)) {
    throw new Error('no active run. start one with: agent-flow run "<task>"');
  }
  return normalizeRunState(root, JSON.parse(fs.readFileSync(pathName, "utf8")));
}

function resolveRunDir(root, runDir) {
  return path.isAbsolute(runDir) ? runDir : path.join(root, runDir);
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
    path.join(root, ".omp", "agents", "code-reviewer.md"),
  ];
  const missing = required.filter((pathName) => !fs.existsSync(pathName));
  if (missing.length > 0) {
    throw new Error(
      `agent-flow is not installed. run: agent-flow-kit install; missing: `
      + missing.map((pathName) => path.relative(root, pathName)).join(", "),
    );
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
  const index = workflowPhases(state.workflow).findIndex((phase) => phase.id === state.phase);
  if (index === -1 || index === state.phase_index) {
    return state;
  }
  const normalized = {
    ...state,
    phase_index: index,
  };
  writeJson(path.join(resolveRunDir(root, state.run_dir), "manifest.json"), normalized);
  writeJson(currentRunPath(root), normalized);
  return normalized;
}

function currentRunPath(root) {
  return path.join(root, ".agent-flow", "state", "current-run.json");
}

function pushWatchStatePath(root) {
  return path.join(root, ".agent-flow", "state", "push-watch.json");
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

function printNext(state, root = null) {
  const phase = workflowPhases(state.workflow)[state.phase_index];
  if (!phase) {
    console.log(`workflow complete: ${state.run_id}`);
    return;
  }
  const localSkillBlock = root ? localSkillPromptBlock(root, phase.id) : "";
  console.log(`Current phase: ${phase.id}`);
  console.log(`Run: ${state.run_id}`);
  console.log(`Workspace root: ${state.workspace_root ?? root ?? process.cwd()}`);
  console.log(`Required artifact: ${path.join(state.run_dir, phase.artifact)}`);
  console.log(`Instruction: ${phase.instruction}${localSkillBlock}`);
}

function printStatus(state, root) {
  const phase = workflowPhases(state.workflow)[state.phase_index];
  const resolvedRunDir = resolveRunDir(root, state.run_dir);
  const complete = state.status === "complete" || state.phase === "complete" || !phase;
  const requiredArtifact = phase ? path.join(state.run_dir, phase.artifact) : null;
  const resolvedRequiredArtifact = phase ? path.join(resolvedRunDir, phase.artifact) : null;
  let status = complete ? "complete" : state.status;
  let reason = complete ? "workflow_complete" : "in_progress";
  if (!complete && resolvedRequiredArtifact && !fs.existsSync(resolvedRequiredArtifact)) {
    status = "awaiting_host";
    reason = "missing_phase_artifact";
  } else if (!complete && requiredArtifact) {
    const missing = missingMarkersForPhase(
      fs.readFileSync(resolvedRequiredArtifact, "utf8"),
      phase,
      root,
    );
    status = "blocked";
    if (artifactIsStale(state, resolvedRequiredArtifact)) {
      reason = "stale_artifact";
    } else if (missing.length > 0) {
      reason = "missing_completion_markers";
    } else {
      try {
        nextPhaseIndex(state, workflowPhases(state.workflow), phase, resolvedRequiredArtifact);
        reason = "phase_artifact_written_advance_required";
      } catch (_error) {
        reason = "route_blocked";
      }
    }
  }
  const nextCommand = complete
    ? "none"
    : reason === "route_blocked"
      ? `${AGENT_FLOW_COMMAND} run next`
      : `${AGENT_FLOW_COMMAND} run advance`;
  const payload = {
    status,
    run: `${state.workflow}/${state.run_id}`,
    task: state.task ?? "",
    current_phase: phase?.id ?? "-",
    workspace_root: state.workspace_root ?? root,
    reason,
    required_artifact: requiredArtifact,
    next_command: nextCommand,
  };
  console.log(`${state.workflow} ${state.run_id} ${status} phase=${phase?.id ?? "-"}`);
  console.log(`status: ${statusValue(status)}`);
  console.log(`run: ${statusValue(payload.run)}`);
  console.log(`task: ${statusValue(payload.task)}`);
  console.log(`current_phase: ${statusValue(payload.current_phase)}`);
  console.log(`workspace_root: ${statusValue(payload.workspace_root)}`);
  console.log(`reason: ${statusValue(reason)}`);
  if (requiredArtifact) {
    console.log(`required_artifact: ${statusValue(requiredArtifact)}`);
  }
  console.log(`next_command: ${statusValue(nextCommand)}`);
  console.log(`status_json: ${JSON.stringify(payload)}`);
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
  withManagedInstallMutation(pathName, () => {
    fs.mkdirSync(path.dirname(pathName), { recursive: true });
    fs.writeFileSync(pathName, content, "utf8");
  });
}

function writeManagedFileIfMissingOrSame(pathName, content, force = false, track = true) {
  const write = () => {
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
  };
  return track ? withManagedInstallMutation(pathName, write) : write();
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
) {
  if (!fs.existsSync(src)) {
    return;
  }
  const copy = () => {
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
        copyBundledDirIfMissingOrSame(srcPath, destPath, force, excludedRootDirs, false, pruneExtraneous, preservedExtraneousRootNames, null);
        continue;
      }
      if (!entry.isFile()) {
        continue;
      }
      const content = fs.readFileSync(srcPath, "utf8");
      writeManagedFileIfMissingOrSame(destPath, content, force, false);
    }
    if (force && pruneExtraneous) {
      for (const entry of fs.readdirSync(dest, { withFileTypes: true })) {
        if (!sourceNames.has(entry.name) && !(isRoot && preservedExtraneousRootNames.has(entry.name))) {
          fs.rmSync(path.join(dest, entry.name), { recursive: true, force: true });
        }
      }
    }
  };
  if (isRoot) withManagedInstallMutation(dest, copy);
  else copy();
}

function removeManagedDirIfSame(src, dest, force = false) {
  if (!fs.existsSync(dest)) {
    return;
  }
  if (!force && !dirContentsMatch(src, dest)) {
    return;
  }
  fs.rmSync(dest, { recursive: true, force: true });
}

function removeStaleContextDocsScripts(agentFlowDir, force = false) {
  if (!force) {
    return;
  }
  withManagedInstallMutation(path.join(agentFlowDir, "scripts"), () => {
    for (const filename of ["check-context-docs.mjs", "check-context-docs.ts"]) {
      fs.rmSync(path.join(agentFlowDir, "scripts", filename), { force: true });
    }
  });
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

function materializeResolvedSkillSources(agentFlowDir, sourcePlan, transaction = null) {
  for (const entry of sourcePlan?.entries || []) {
    if (!["host-bootstrap", "shared"].includes(entry.source_kind)) continue;
    const destination = path.join(agentFlowDir, "skills", entry.name);
    let sourcePath = entry.source_path;
    if (samePath(sourcePath, destination)) {
      const backupSource = transaction ? path.join(transaction.backup, entry.name) : null;
      if (!backupSource || !fs.existsSync(backupSource)) continue;
      sourcePath = backupSource;
    }
    const stage = path.join(
      agentFlowDir,
      "skills",
      `.agent-flow-stage-${entry.name}-${process.pid}-${crypto.randomBytes(6).toString("hex")}`,
    );
    try {
      fs.cpSync(sourcePath, stage, { recursive: true, dereference: false, errorOnExist: true });
      if (hashSkillTree(stage) !== entry.tree_hash) {
        throw new Error(`skill source changed while copying: ${entry.name}`);
      }
      if (fs.existsSync(destination)) fs.rmSync(destination, { recursive: true, force: true });
      fs.renameSync(stage, destination);
      if (hashSkillTree(destination) !== entry.tree_hash) {
        throw new Error(`installed skill snapshot integrity mismatch: ${entry.name}`);
      }
    } finally {
      if (fs.existsSync(stage)) fs.rmSync(stage, { recursive: true, force: true });
    }
  }
}

function installProjectSkills(
  root,
  agentFlowDir,
  previousIndex,
  force = false,
  installSelection = null,
  sourcePlan = null,
  transaction = null,
) {
  const selected = selectProjectSkills(root, agentFlowDir, installSelection, sourcePlan);
  const links = [];
  for (const skill of selected.skills) {
    // bundled skill 중 host 디렉토리 link 대상은 BUNDLED_HOST_SKILL_NAMES뿐이다.
    // 나머지 bundled skill은 index에만 노출해 agent가 발견할 수 있게 한다.
    if (skill.source === "bundled" && !BUNDLED_HOST_SKILL_NAMES.has(skill.name)) {
      continue;
    }
    for (const host of skill.hosts) {
      links.push(linkProjectSkill(root, skill, host, previousIndex, force, transaction));
    }
  }
  links.push(...removeStaleProjectSkillLinks(root, selected.skills, previousIndex, force, transaction));
  const index = { ...selected, links };
  index.catalog_fingerprint = skillCatalogFingerprint(root, HOME, detectActiveHost(process.env), process.env);
  fs.writeFileSync(
    path.join(agentFlowDir, "skills", "index.json"),
    `${JSON.stringify(index, null, 2)}\n`,
    "utf8",
  );
  return index;
}

function refreshSkillCatalogAtBoundary(root) {
  if (!root) return;
  const leaderRoot = resolveManagedWorktreeRoot(root) || root;
  const indexPath = path.join(leaderRoot, ".agent-flow", "skills", "index.json");
  const index = readJsonIfExists(indexPath);
  if (!index?.catalog_fingerprint) return;
  const current = skillCatalogFingerprint(leaderRoot, HOME, detectActiveHost(process.env), process.env);
  if (current === index.catalog_fingerprint) return;
  installProject(leaderRoot);
}

function acquireProjectInstallLock(root, agentFlowDir) {
  const lockPath = path.join(agentFlowDir, "install.lock");
  if (fs.existsSync(lockPath)) {
    const ownerPath = path.join(lockPath, "owner.json");
    const owner = readJsonIfExists(ownerPath);
    const validOwner = owner?.version === 1
      && owner.root === fs.realpathSync(root)
      && Number.isInteger(owner.pid)
      && typeof owner.token === "string";
    if (!validOwner || processIsAlive(owner.pid)) {
      throw new Error(`project install lock is held: ${lockPath}`);
    }
    fs.rmSync(lockPath, { recursive: true, force: true });
  }
  fs.mkdirSync(lockPath);
  const lock = {
    version: 1,
    root: fs.realpathSync(root),
    pid: process.pid,
    token: crypto.randomBytes(24).toString("hex"),
    acquired_at: new Date().toISOString(),
  };
  fs.writeFileSync(path.join(lockPath, "owner.json"), `${JSON.stringify(lock, null, 2)}\n`, { flag: "wx" });
  return { ...lock, path: lockPath };
}

function processIsAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error?.code !== "ESRCH";
  }
}

function holdInstallForTest(name, marker) {
  const milliseconds = Number.parseInt(process.env[name] || "0", 10);
  if (!Number.isInteger(milliseconds) || milliseconds <= 0 || milliseconds > 10_000) return;
  process.stderr.write(`agent-flow:test-${marker}\n`);
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, milliseconds);
}

function releaseProjectInstallLock(lock) {
  if (!lock || !fs.existsSync(lock.path)) return;
  const owner = readJsonIfExists(path.join(lock.path, "owner.json"));
  if (owner?.token !== lock.token || owner?.pid !== process.pid) {
    throw new Error(`project install lock ownership changed: ${lock.path}`);
  }
  fs.rmSync(lock.path, { recursive: true, force: true });
}

function readAuthenticatedSkillIndex(agentFlowDir, existingKit = null) {
  const indexPath = path.join(agentFlowDir, "skills", "index.json");
  if (!fs.existsSync(indexPath)) return null;
  const noFollow = fs.constants.O_NOFOLLOW || 0;
  const descriptor = fs.openSync(indexPath, fs.constants.O_RDONLY | noFollow);
  try {
    const before = fs.fstatSync(descriptor);
    if (!before.isFile() || before.nlink !== 1) throw new Error(`invalid previous skill index file: ${indexPath}`);
    const bytes = fs.readFileSync(descriptor);
    const after = fs.fstatSync(descriptor);
    if (
      before.dev !== after.dev
      || before.ino !== after.ino
      || before.size !== after.size
      || before.mtimeMs !== after.mtimeMs
    ) throw new Error(`previous skill index changed during authentication: ${indexPath}`);
    let payload;
    try {
      payload = JSON.parse(bytes.toString("utf8"));
    } catch {
      throw new Error(`invalid previous skill index JSON: ${indexPath}`);
    }
    if (!Array.isArray(payload?.skills) || !Array.isArray(payload?.links)) {
      throw new Error(`unsupported previous skill index: ${indexPath}`);
    }
    if (payload.version === undefined) payload = { version: 1, selection: {}, ...payload };
    if (![1, 2].includes(payload.version)) throw new Error(`unsupported previous skill index: ${indexPath}`);
    const hasIndexCommitment = existingKit?.skill_index_hash_version === 1
      && typeof existingKit?.skill_index_hash === "string";
    if (hasIndexCommitment) {
      const indexHash = crypto.createHash("sha256").update(bytes).digest("hex");
      if (indexHash !== existingKit.skill_index_hash) {
        throw new Error("previous skill index does not match kit commitment");
      }
      if (
        existingKit.skill_plan_hash_version !== 2
        || computeSkillPlanHash(payload, path.dirname(agentFlowDir), false) !== existingKit.skill_plan_hash
      ) {
        throw new Error("previous skill plan does not match kit commitment");
      }
      if (
        existingKit.skill_links_commitment_version !== SKILL_LINKS_COMMITMENT_VERSION
        || skillLinksCommitment(existingKit.skill_plan_hash, payload.links)
          !== existingKit.skill_links_commitment
      ) {
        throw new Error("previous skill links do not match kit commitment");
      }
    } else {
      payload = { ...payload, links: [] };
    }
    return {
      payload,
      bytes,
      hash: crypto.createHash("sha256").update(bytes).digest("hex"),
      identity: { dev: before.dev, ino: before.ino, size: before.size, mtimeMs: before.mtimeMs },
    };
  } finally {
    fs.closeSync(descriptor);
  }
}

function beginSkillInstallTransaction(root, agentFlowDir, previousIndexRecord) {
  const transactionRoot = path.join(agentFlowDir, "install-transaction");
  if (fs.existsSync(transactionRoot)) throw new Error(`open skill install transaction: ${transactionRoot}`);
  const live = path.join(agentFlowDir, "skills");
  if (fs.existsSync(live) && !previousIndexRecord) {
    throw new Error("existing skills directory has no authenticated index");
  }
  fs.mkdirSync(transactionRoot);
  const backup = path.join(transactionRoot, "skills-backup");
  const marker = path.join(live, ".agent-flow-transaction-owner");
  const journalPath = path.join(transactionRoot, "journal.json");
  const transaction = {
    root,
    transactionRoot,
    live,
    backup,
    marker,
    journalPath,
    token: crypto.randomBytes(24).toString("hex"),
    previous: previousIndexRecord,
  };
  const journal = {
    version: 2,
    root: fs.realpathSync(root),
    token: transaction.token,
    stage: "prepared",
    previous_index_hash: previousIndexRecord?.hash || null,
    previous_index_bytes: previousIndexRecord?.bytes.toString("base64") || null,
    had_live_skills: fs.existsSync(live),
    host_mutations: [],
    managed_mutations: [],
  };
  writeInstallJournal(journalPath, journal);
  if (journal.had_live_skills) {
    if (!previousIndexRecord) {
      throw new Error("existing skills directory has no authenticated index");
    }
    journal.stage = "moving-skills";
    writeInstallJournal(journalPath, journal);
    fs.renameSync(live, backup);
    if (process.env.AGENT_FLOW_TEST_CRASH_AFTER_SKILLS_RENAME === "1") process.exit(88);
    journal.stage = "skills-moved";
    writeInstallJournal(journalPath, journal);
    if (process.env.AGENT_FLOW_TEST_CRASH_AFTER_SKILLS_MOVE === "1") process.exit(86);
    const backupIndex = fs.readFileSync(path.join(backup, "index.json"));
    const backupHash = crypto.createHash("sha256").update(backupIndex).digest("hex");
    if (backupHash !== previousIndexRecord.hash || !backupIndex.equals(previousIndexRecord.bytes)) {
      throw new Error("previous skill index changed after authentication; backup was not adopted");
    }
  }
  fs.mkdirSync(live, { recursive: true });
  fs.writeFileSync(marker, `${transaction.token}\n`, { flag: "wx" });
  journal.stage = "live-created";
  writeInstallJournal(journalPath, journal);
  holdInstallForTest("AGENT_FLOW_TEST_HOLD_INSTALL_LOCK_MS", "install-lock-held");
  transaction.journal = journal;
  snapshotManagedInstallPaths(transaction);
  activeManagedInstallTransaction = transaction;
  return transaction;
}

function writeInstallJournal(journalPath, journal) {
  const temporary = `${journalPath}.tmp-${process.pid}-${crypto.randomBytes(6).toString("hex")}`;
  fs.writeFileSync(temporary, `${JSON.stringify(journal, null, 2)}\n`, { flag: "wx" });
  fs.renameSync(temporary, journalPath);
}

function hostPathState(pathName) {
  const stat = lstatIfExists(pathName);
  if (!stat) return { kind: "absent" };
  if (stat.isSymbolicLink()) return { kind: "symlink", target: fs.readlinkSync(pathName) };
  if (stat.isDirectory()) return { kind: "directory", tree_hash: hashSkillTree(pathName) };
  if (stat.isFile()) {
    return {
      kind: "file",
      file_hash: crypto.createHash("sha256").update(fs.readFileSync(pathName)).digest("hex"),
    };
  }
  throw new Error(`unsupported host skill path kind: ${pathName}`);
}

function sameHostPathState(left, right) {
  if (!left || !right || left.kind !== right.kind) return false;
  if (left.kind === "symlink") return left.target === right.target;
  if (left.kind === "directory") return left.tree_hash === right.tree_hash;
  if (left.kind === "file") return left.file_hash === right.file_hash;
  return left.kind === "absent";
}

function withHostPathMutation(transaction, target, allowedAfter, callback) {
  if (!transaction) return callback();
  ensureChildPath(transaction.root, target);
  const relative = path.relative(transaction.root, target);
  const before = hostPathState(target);
  const operationId = transaction.journal.host_mutations.length;
  if (["directory", "file"].includes(before.kind)) {
    const backupRelative = path.join("host-backups", String(operationId));
    const backupPath = path.join(transaction.transactionRoot, backupRelative);
    fs.mkdirSync(path.dirname(backupPath), { recursive: true });
    fs.cpSync(target, backupPath, { recursive: true, dereference: false, errorOnExist: true });
    if (!sameHostPathState(before, hostPathState(backupPath))) {
      throw new Error(`host skill backup integrity mismatch: ${relative}`);
    }
    before.backup = backupRelative;
  }
  const operation = {
    path: relative,
    before,
    allowed_after: allowedAfter,
    after: null,
  };
  transaction.journal.host_mutations.push(operation);
  writeInstallJournal(transaction.journalPath, transaction.journal);
  let result;
  let callbackError = null;
  try {
    result = callback();
  } catch (error) {
    callbackError = error;
  }
  operation.after = hostPathState(target);
  writeInstallJournal(transaction.journalPath, transaction.journal);
  if (callbackError) throw callbackError;
  if (!allowedAfter.some((state) => sameHostPathState(state, operation.after))) {
    throw new Error(`unexpected host skill mutation result: ${relative}`);
  }
  return result;
}

function hostMutationTarget(root, relative) {
  if (typeof relative !== "string" || path.isAbsolute(relative)) {
    throw new Error("invalid host skill mutation path");
  }
  const target = path.resolve(root, relative);
  ensureChildPath(root, target);
  return target;
}

function restoreHostPathState(target, state, transactionRoot) {
  if (lstatIfExists(target)) fs.rmSync(target, { recursive: true, force: true });
  if (state.kind === "absent") return;
  fs.mkdirSync(path.dirname(target), { recursive: true });
  if (state.kind === "symlink") {
    fs.symlinkSync(state.target, target, "dir");
    return;
  }
  if (!["directory", "file"].includes(state.kind) || typeof state.backup !== "string") {
    throw new Error(`invalid host skill backup state: ${target}`);
  }
  const backupPath = path.resolve(transactionRoot, state.backup);
  ensureChildPath(transactionRoot, backupPath);
  if (!sameHostPathState(state, hostPathState(backupPath))) {
    throw new Error(`host skill backup authentication failed: ${target}`);
  }
  fs.cpSync(backupPath, target, { recursive: true, dereference: false, errorOnExist: true });
}

function rollbackRecordedHostMutations(root, transactionRoot, journal) {
  const operations = Array.isArray(journal?.host_mutations) ? journal.host_mutations : [];
  for (let index = operations.length - 1; index >= 0; index -= 1) {
    const operation = operations[index];
    const target = hostMutationTarget(root, operation.path);
    const current = hostPathState(target);
    if (sameHostPathState(current, operation.before)) {
      operation.rolled_back = true;
      continue;
    }
    const committedByTransaction = operation.after
      ? sameHostPathState(current, operation.after)
      : (operation.allowed_after || []).some((state) => sameHostPathState(current, state));
    if (!committedByTransaction) {
      throw new Error(`host skill path changed outside install transaction: ${operation.path}`);
    }
    restoreHostPathState(target, operation.before, transactionRoot);
    if (!sameHostPathState(hostPathState(target), operation.before)) {
      throw new Error(`host skill rollback integrity mismatch: ${operation.path}`);
    }
    operation.rolled_back = true;
  }
  if (fs.existsSync(transactionRoot)) {
    writeInstallJournal(path.join(transactionRoot, "journal.json"), journal);
  }
}

function verifyCommittedHostMutations(root, journal) {
  for (const operation of journal?.host_mutations || []) {
    if (!operation.after) throw new Error(`incomplete host skill mutation: ${operation.path}`);
    const target = hostMutationTarget(root, operation.path);
    if (!sameHostPathState(hostPathState(target), operation.after)) {
      throw new Error(`host skill mutation commitment changed: ${operation.path}`);
    }
  }
}

function managedInstallPaths(root) {
  const agentFlowDir = path.join(root, ".agent-flow");
  return [
    ...["workflows", "profiles", "templates", "scripts", "prompts", "rules", "bootstrap"]
      .map((name) => path.join(agentFlowDir, name)),
    path.join(root, RUNTIME_PYTHON_RELATIVE, "agent_flow"),
    path.join(agentFlowDir, "kit.json"),
    path.join(root, "scripts"),
    path.join(root, ".Codex", "agents"),
    path.join(root, ".claude", "agents"),
    path.join(root, ".omp", "agents"),
    path.join(root, ".Codex", "rules", "context"),
    path.join(root, ".Codex", "context"),
    path.join(root, ".Codex", "rules", "codebase-rubric.md"),
    path.join(root, ".Codex", "rules", "concise-output.md"),
    path.join(root, ".Codex", "hooks.json"),
    path.join(root, ".codex", "hooks.json"),
    path.join(root, ".claude", "settings.json"),
    path.join(root, ".omp", "extensions", "agent-flow-hooks.ts"),
    path.join(root, ".gitignore"),
    path.join(root, "AGENTS.md"),
    path.join(root, "CLAUDE.md"),
  ];
}

function managedPathState(pathName) {
  const stat = lstatIfExists(pathName);
  if (!stat) return { kind: "absent" };
  if (stat.isSymbolicLink()) {
    return { kind: "symlink", target: fs.readlinkSync(pathName) };
  }
  if (stat.isFile()) {
    return {
      kind: "file",
      mode: stat.mode & 0o777,
      hash: crypto.createHash("sha256").update(fs.readFileSync(pathName)).digest("hex"),
    };
  }
  if (!stat.isDirectory()) throw new Error(`unsupported managed install path kind: ${pathName}`);
  const entries = [];
  const visit = (current, relative) => {
    const currentStat = fs.lstatSync(current);
    entries.push({ path: relative, kind: "directory", mode: currentStat.mode & 0o777 });
    for (const name of fs.readdirSync(current).sort()) {
      const child = path.join(current, name);
      const childRelative = relative ? `${relative}/${name}` : name;
      const childStat = fs.lstatSync(child);
      if (childStat.isSymbolicLink()) {
        entries.push({ path: childRelative, kind: "symlink", target: fs.readlinkSync(child) });
      } else if (childStat.isDirectory()) {
        visit(child, childRelative);
      } else if (childStat.isFile()) {
        entries.push({
          path: childRelative,
          kind: "file",
          mode: childStat.mode & 0o777,
          hash: crypto.createHash("sha256").update(fs.readFileSync(child)).digest("hex"),
        });
      } else {
        throw new Error(`unsupported managed install tree entry: ${child}`);
      }
    }
  };
  visit(pathName, "");
  return {
    kind: "directory",
    commitment: crypto.createHash("sha256").update(JSON.stringify(entries)).digest("hex"),
  };
}

function sameManagedPathState(left, right) {
  if (!left || !right || left.kind !== right.kind) return false;
  if (left.kind === "symlink") return left.target === right.target;
  if (left.kind === "file") return left.mode === right.mode && left.hash === right.hash;
  if (left.kind === "directory") return left.commitment === right.commitment;
  return left.kind === "absent";
}

function managedInstallMutationOperation(transaction, requestedPath) {
  if (!transaction?.journal) return null;
  const requested = path.resolve(requestedPath);
  const caseInsensitive = process.platform === "darwin" || process.platform === "win32";
  const matches = (transaction.journal.managed_mutations || []).filter((operation) => {
    const target = path.resolve(transaction.root, operation.path);
    if (caseInsensitive) {
      const foldedTarget = target.toLowerCase();
      const foldedRequested = requested.toLowerCase();
      return foldedRequested === foldedTarget || foldedRequested.startsWith(`${foldedTarget}${path.sep}`);
    }
    const relative = path.relative(target, requested);
    return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
  });
  matches.sort((left, right) => right.path.length - left.path.length);
  return matches[0] ?? null;
}

function withManagedInstallMutation(pathName, callback) {
  const transaction = activeManagedInstallTransaction;
  const operation = managedInstallMutationOperation(transaction, pathName);
  if (!operation) return callback();
  const target = path.resolve(transaction.root, operation.path);
  const expected = operation.after ?? operation.before;
  if (!sameManagedPathState(managedPathState(target), expected)) {
    throw new Error(`managed install path changed outside transaction: ${operation.path}`);
  }
  const result = callback();
  operation.after = managedPathState(target);
  writeInstallJournal(transaction.journalPath, transaction.journal);
  return result;
}

function snapshotManagedInstallPaths(transaction) {
  for (const target of managedInstallPaths(transaction.root)) {
    ensureChildPath(transaction.root, target);
    const before = managedPathState(target);
    const operation = {
      path: path.relative(transaction.root, target),
      before,
      after: null,
    };
    if (["directory", "file"].includes(before.kind)) {
      const backupRelative = path.join("managed-backups", String(transaction.journal.managed_mutations.length));
      const backupPath = path.join(transaction.transactionRoot, backupRelative);
      fs.mkdirSync(path.dirname(backupPath), { recursive: true });
      fs.cpSync(target, backupPath, { recursive: true, dereference: false, errorOnExist: true });
      if (!sameManagedPathState(before, managedPathState(backupPath))) {
        throw new Error(`managed install backup integrity mismatch: ${operation.path}`);
      }
      before.backup = backupRelative;
    }
    transaction.journal.managed_mutations.push(operation);
  }
  writeInstallJournal(transaction.journalPath, transaction.journal);
}

function sealManagedInstallMutations(transaction) {
  if (!transaction || !transaction.journal || !fs.existsSync(transaction.transactionRoot)) return;
  for (const operation of transaction.journal.managed_mutations || []) {
    const current = managedPathState(hostMutationTarget(transaction.root, operation.path));
    if (operation.after && !sameManagedPathState(operation.after, current)) {
      throw new Error(`managed install path changed outside transaction: ${operation.path}`);
    }
    operation.after = current;
  }
  writeInstallJournal(transaction.journalPath, transaction.journal);
}

function restoreManagedPathState(target, state, transactionRoot) {
  if (lstatIfExists(target)) fs.rmSync(target, { recursive: true, force: true });
  if (state.kind === "absent") return;
  fs.mkdirSync(path.dirname(target), { recursive: true });
  if (state.kind === "symlink") {
    fs.symlinkSync(state.target, target);
    return;
  }
  if (!["directory", "file"].includes(state.kind) || typeof state.backup !== "string") {
    throw new Error(`invalid managed install backup state: ${target}`);
  }
  const backupPath = path.resolve(transactionRoot, state.backup);
  ensureChildPath(transactionRoot, backupPath);
  if (!sameManagedPathState(state, managedPathState(backupPath))) {
    throw new Error(`managed install backup authentication failed: ${target}`);
  }
  fs.cpSync(backupPath, target, { recursive: true, dereference: false, errorOnExist: true });
}

function rollbackRecordedManagedMutations(root, transactionRoot, journal) {
  const operations = Array.isArray(journal?.managed_mutations) ? journal.managed_mutations : [];
  for (let index = operations.length - 1; index >= 0; index -= 1) {
    const operation = operations[index];
    const target = hostMutationTarget(root, operation.path);
    const current = managedPathState(target);
    if (sameManagedPathState(current, operation.before)) continue;
    if (!operation.after || !sameManagedPathState(current, operation.after)) {
      throw new Error(`managed install path changed outside transaction: ${operation.path}`);
    }
    restoreManagedPathState(target, operation.before, transactionRoot);
    if (!sameManagedPathState(managedPathState(target), operation.before)) {
      throw new Error(`managed install rollback integrity mismatch: ${operation.path}`);
    }
  }
  if (fs.existsSync(transactionRoot)) {
    writeInstallJournal(path.join(transactionRoot, "journal.json"), journal);
  }
}

function verifyCommittedManagedMutations(root, journal) {
  for (const operation of journal?.managed_mutations || []) {
    if (!operation.after) throw new Error(`incomplete managed install mutation: ${operation.path}`);
    const target = hostMutationTarget(root, operation.path);
    if (!sameManagedPathState(managedPathState(target), operation.after)) {
      throw new Error(`managed install commitment changed: ${operation.path}`);
    }
  }
}

function commitSkillInstallTransaction(transaction) {
  if (!transaction) return;
  sealManagedInstallMutations(transaction);
  const indexPath = path.join(transaction.live, "index.json");
  if (!fs.existsSync(indexPath)) throw new Error("skill install transaction produced no index");
  const marker = fs.readFileSync(transaction.marker, "utf8").trim();
  if (marker !== transaction.token) throw new Error("skill install transaction ownership changed");
  verifyCommittedHostMutations(transaction.root, transaction.journal);
  verifyCommittedManagedMutations(transaction.root, transaction.journal);
  transaction.journal.stage = "committed";
  transaction.journal.committed_index_hash = crypto.createHash("sha256")
    .update(fs.readFileSync(indexPath))
    .digest("hex");
  writeInstallJournal(transaction.journalPath, transaction.journal);
  fs.unlinkSync(transaction.marker);
  fs.rmSync(transaction.transactionRoot, { recursive: true, force: true });
  if (activeManagedInstallTransaction === transaction) activeManagedInstallTransaction = null;
}

function rollbackSkillInstallTransaction(transaction) {
  if (!transaction || !fs.existsSync(transaction.transactionRoot)) return;
  let hostRollbackError = null;
  let managedRollbackError = null;
  try {
    rollbackRecordedManagedMutations(transaction.root, transaction.transactionRoot, transaction.journal);
  } catch (error) {
    managedRollbackError = error;
  }
  try {
    rollbackRecordedHostMutations(transaction.root, transaction.transactionRoot, transaction.journal);
  } catch (error) {
    hostRollbackError = error;
  }
  const liveMarker = fs.existsSync(transaction.marker)
    ? fs.readFileSync(transaction.marker, "utf8").trim()
    : null;
  if (fs.existsSync(transaction.live)) {
    if (liveMarker !== transaction.token) {
      throw new Error(`cannot roll back unowned live skills directory: ${transaction.live}`);
    }
    fs.rmSync(transaction.live, { recursive: true, force: true });
  }
  if (fs.existsSync(transaction.backup)) {
    const backupIndex = fs.readFileSync(path.join(transaction.backup, "index.json"));
    const hash = crypto.createHash("sha256").update(backupIndex).digest("hex");
    if (hash !== transaction.previous?.hash || !backupIndex.equals(transaction.previous.bytes)) {
      throw new Error("cannot restore unauthenticated skill index backup");
    }
    fs.renameSync(transaction.backup, transaction.live);
  }
  if (hostRollbackError || managedRollbackError) {
    transaction.journal.stage = "rollback-blocked";
    writeInstallJournal(transaction.journalPath, transaction.journal);
    throw hostRollbackError || managedRollbackError;
  }
  fs.rmSync(transaction.transactionRoot, { recursive: true, force: true });
  if (activeManagedInstallTransaction === transaction) activeManagedInstallTransaction = null;
}

function preserveUnmanagedSkillEntries(transaction, previousIndex, currentIndex) {
  if (!transaction || !fs.existsSync(transaction.backup)) return;
  const managed = new Set(["index.json"]);
  for (const entry of fs.readdirSync(path.join(KIT_ROOT, "skills"), { withFileTypes: true })) {
    if (!entry.isDirectory()) managed.add(entry.name);
  }
  for (const skill of previousIndex?.skills || []) {
    const relative = String(skill?.path || "").replaceAll("\\", "/");
    const prefix = ".agent-flow/skills/";
    if (!relative.startsWith(prefix)) continue;
    const rootName = relative.slice(prefix.length).split("/")[0];
    if (rootName) managed.add(rootName);
  }
  for (const entry of fs.readdirSync(transaction.backup)) {
    if (managed.has(entry) || entry === ".agent-flow-transaction-owner") continue;
    const source = path.join(transaction.backup, entry);
    const destination = path.join(transaction.live, entry);
    if (lstatIfExists(destination)) {
      throw new Error(`unmanaged skill entry conflicts with installed skill: ${entry}`);
    }
    fs.cpSync(source, destination, { recursive: true, dereference: false, errorOnExist: true });
  }
  const indexed = new Set((currentIndex?.skills || []).map((skill) => skill.name));
  for (const entry of fs.readdirSync(transaction.live, { withFileTypes: true })) {
    if (managed.has(entry.name) || indexed.has(entry.name) || entry.name.startsWith(".")) continue;
    if (entry.isDirectory() && fs.existsSync(path.join(transaction.live, entry.name, "SKILL.md"))) {
      currentIndex.warnings.push(`${entry.name}: preserved unmanaged skill entry without adopting ownership`);
    }
  }
  fs.writeFileSync(
    path.join(transaction.live, "index.json"),
    `${JSON.stringify(currentIndex, null, 2)}\n`,
    "utf8",
  );
}

function recoverInterruptedSkillTransaction(root, agentFlowDir) {
  const transactionRoot = path.join(agentFlowDir, "install-transaction");
  if (!fs.existsSync(transactionRoot)) return;
  const journalPath = path.join(transactionRoot, "journal.json");
  const journal = readJsonIfExists(journalPath);
  if (
    ![1, 2].includes(journal?.version)
    || journal.root !== fs.realpathSync(root)
    || typeof journal.token !== "string"
    || !["prepared", "moving-skills", "skills-moved", "live-created", "committed", "recovered", "rollback-blocked"].includes(journal.stage)
  ) throw new Error(`invalid interrupted skill transaction: ${transactionRoot}`);
  const live = path.join(agentFlowDir, "skills");
  const backup = path.join(transactionRoot, "skills-backup");
  const marker = path.join(live, ".agent-flow-transaction-owner");
  if (journal.stage === "rollback-blocked") {
    rollbackRecordedManagedMutations(root, transactionRoot, journal);
    rollbackRecordedHostMutations(root, transactionRoot, journal);
    if (!journal.had_live_skills) {
      if (fs.existsSync(live)) {
        throw new Error("blocked initial skill transaction unexpectedly has a live directory");
      }
      fs.rmSync(transactionRoot, { recursive: true, force: true });
      return;
    }
    const authenticatedBytes = Buffer.from(String(journal.previous_index_bytes || ""), "base64");
    const liveIndex = fs.readFileSync(path.join(live, "index.json"));
    if (!liveIndex.equals(authenticatedBytes)) {
      throw new Error("blocked skill transaction live index is not authenticated");
    }
    fs.rmSync(transactionRoot, { recursive: true, force: true });
    return;
  }
  if (journal.stage === "committed") {
    if (!fs.existsSync(path.join(live, "index.json"))) {
      throw new Error("committed skill transaction has no live index");
    }
    verifyCommittedHostMutations(root, journal);
    verifyCommittedManagedMutations(root, journal);
    if (fs.existsSync(marker)) {
      if (fs.readFileSync(marker, "utf8").trim() !== journal.token) {
        throw new Error("committed skill transaction marker is not owned");
      }
      fs.unlinkSync(marker);
    }
    fs.rmSync(transactionRoot, { recursive: true, force: true });
    return;
  }
  if (journal.stage === "prepared") {
    if (
      journal.had_live_skills
      && !fs.existsSync(path.join(live, "index.json"))
      && fs.existsSync(path.join(backup, "index.json"))
    ) {
      journal.stage = "skills-moved";
    } else {
      if (journal.had_live_skills && !fs.existsSync(path.join(live, "index.json"))) {
        throw new Error("prepared skill transaction lost its live index");
      }
      fs.rmSync(transactionRoot, { recursive: true, force: true });
      return;
    }
  }
  if (journal.stage === "moving-skills") {
    const liveExists = fs.existsSync(path.join(live, "index.json"));
    const backupExists = fs.existsSync(path.join(backup, "index.json"));
    if (liveExists && !backupExists) {
      fs.rmSync(transactionRoot, { recursive: true, force: true });
      return;
    }
    if (!liveExists && backupExists) {
      journal.stage = "skills-moved";
    } else {
      throw new Error("interrupted skill move has ambiguous live and backup state");
    }
  }
  if (!journal.had_live_skills) {
    rollbackRecordedManagedMutations(root, transactionRoot, journal);
    rollbackRecordedHostMutations(root, transactionRoot, journal);
    if (fs.existsSync(live)) {
      const liveToken = fs.existsSync(marker) ? fs.readFileSync(marker, "utf8").trim() : null;
      if (liveToken !== journal.token) {
        throw new Error("interrupted initial skill transaction live directory is not owned");
      }
      fs.rmSync(live, { recursive: true, force: true });
    }
    journal.stage = "recovered";
    writeInstallJournal(journalPath, journal);
    fs.rmSync(transactionRoot, { recursive: true, force: true });
    return;
  }
  if (!fs.existsSync(backup) || typeof journal.previous_index_hash !== "string") {
    throw new Error("interrupted skill transaction backup is incomplete");
  }
  rollbackRecordedManagedMutations(root, transactionRoot, journal);
  rollbackRecordedHostMutations(root, transactionRoot, journal);
  const backupIndex = fs.readFileSync(path.join(backup, "index.json"));
  const authenticatedBytes = Buffer.from(String(journal.previous_index_bytes || ""), "base64");
  const backupHash = crypto.createHash("sha256").update(backupIndex).digest("hex");
  if (backupHash !== journal.previous_index_hash || !backupIndex.equals(authenticatedBytes)) {
    throw new Error("interrupted skill transaction backup is not authenticated");
  }
  if (fs.existsSync(live)) {
    const liveToken = fs.existsSync(marker) ? fs.readFileSync(marker, "utf8").trim() : null;
    if (liveToken !== journal.token) {
      throw new Error("interrupted skill transaction live directory is not owned");
    }
    fs.rmSync(live, { recursive: true, force: true });
  }
  fs.renameSync(backup, live);
  const restored = fs.readFileSync(path.join(live, "index.json"));
  if (!restored.equals(authenticatedBytes)) throw new Error("interrupted skill transaction restore mismatch");
  journal.stage = "recovered";
  writeInstallJournal(journalPath, journal);
  fs.rmSync(transactionRoot, { recursive: true, force: true });
}

function skillCatalogFingerprint(root, home, activeHost, env) {
  const resolvedHome = path.resolve(home || ".");
  const configured = {
    codex: configuredCatalogRoot(env.CODEX_HOME, resolvedHome, ".codex"),
    claude: configuredCatalogRoot(env.CLAUDE_CONFIG_DIR, resolvedHome, ".claude"),
    omp: configuredCatalogRoot(env.PI_CODING_AGENT_DIR, resolvedHome, path.join(".omp", "agent")),
  };
  const roots = [
    ["project-local", path.join(root, ".agent-flow", "local-skills")],
    ["project", samePath(root, KIT_ROOT) ? null : path.join(root, "skills")],
    ["active-host", activeHost ? path.join(configured[activeHost], "skills") : null],
    ["shared", path.join(resolvedHome, ".agents", "skills")],
    ["bundled", path.join(KIT_ROOT, "skills")],
  ];
  const manifest = { active_host: activeHost, roots: [] };
  for (const [source, catalogRoot] of roots) {
    if (!catalogRoot) continue;
    manifest.roots.push({ source, root: path.resolve(catalogRoot), entries: catalogTreeManifest(catalogRoot) });
  }
  return crypto.createHash("sha256").update(JSON.stringify(manifest)).digest("hex");
}

function configuredCatalogRoot(value, home, fallback) {
  if (typeof value !== "string" || !value.trim()) return path.join(home, fallback);
  if (value.trim() === "~") return home;
  if (value.trim().startsWith("~/") || value.trim().startsWith("~\\")) {
    return path.resolve(home, value.trim().slice(2));
  }
  return path.resolve(value.trim());
}

function catalogTreeManifest(root) {
  const stat = lstatIfExists(root);
  if (!stat) return [{ path: "", kind: "missing" }];
  return catalogTreeEntries(root, root);
}

function catalogTreeEntries(root, current) {
  const stat = fs.lstatSync(current);
  const relative = path.relative(root, current).split(path.sep).join("/");
  if (stat.isSymbolicLink()) {
    return [{ path: relative, kind: "symlink", target: fs.readlinkSync(current) }];
  }
  if (stat.isFile()) {
    return [{
      path: relative,
      kind: "file",
      hash: crypto.createHash("sha256").update(fs.readFileSync(current)).digest("hex"),
    }];
  }
  if (!stat.isDirectory()) return [{ path: relative, kind: "other", mode: stat.mode }];
  const result = [{ path: relative, kind: "directory" }];
  for (const entry of fs.readdirSync(current).sort()) {
    result.push(...catalogTreeEntries(root, path.join(current, entry)));
  }
  return result;
}

function selectProjectSkills(root, agentFlowDir, installSelection = null, sourcePlan = null) {
  const discovered = [
    ...discoverSkills(path.join(agentFlowDir, "local-skills"), "local", root, PROFILE_MANAGED_HOST_ONLY_SKILLS),
    ...discoverProjectSkills(root),
    ...discoverSkills(
      path.join(agentFlowDir, "skills"),
      "bundled",
      root,
      new Set(["index.json", ...PROFILE_MANAGED_HOST_ONLY_SKILLS]),
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
  const sourceByName = new Map((sourcePlan?.entries || []).map((entry) => [entry.name, entry]));
  const skills = [...byName.values()]
    .filter((skill) => !allowed || allowed.has(skill.name))
    .map((skill) => {
      const resolved = sourceByName.get(skill.name);
      if (!resolved) return { ...skill, tree_hash: hashSkillTree(path.dirname(path.join(root, skill.path))) };
      return {
        ...skill,
        source: resolved.source_kind,
        source_host: resolved.source_host,
        tree_hash: resolved.tree_hash,
        activation: resolved.automatic_on_demand && !skill.activationDeclared ? "on-demand" : skill.activation,
      };
    })
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
  const selection = {
      mode: allowed ? "filtered" : "all",
      profile_selection: installSelection?.profileSelection || "auto",
      profiles: installSelection?.profiles || [],
      skill_profiles: installSelection?.skillProfiles || installSelection?.profiles || [],
      explicit_skills: installSelection?.explicitSkills || [],
      external_exposure_skills: (sourcePlan?.entries || [])
        .filter((entry) => entry.automatic_on_demand)
        .map((entry) => entry.name)
        .sort(),
      required_review: installSelection?.requiredReview || {},
      conditional_skills: installSelection?.conditionalSkills || {},
      profile_routing: installSelection?.profileRouting || { version: 1, profiles: {}, escalations: {} },
  };
  const indexedSkills = skills.map(({ priority, warnings: _warnings, ...skill }) => skill);
  const revision = crypto.createHash("sha256").update(JSON.stringify({
    selection,
    skills: indexedSkills.map((skill) => ({
      name: skill.name,
      source: skill.source,
      tree_hash: skill.tree_hash,
      activation: skill.activation || "on-demand",
      workflowPhases: skill.workflowPhases,
      taskTerms: skill.taskTerms,
      pathGlobs: skill.pathGlobs,
      requires: skill.requires,
    })),
  })).digest("hex");
  return {
    version: 2,
    revision,
    selection: {
      ...selection,
    },
    skills: indexedSkills,
    conflicts,
    warnings,
  };
}

function computeSkillPlanHash(index, root, verifyTrees = false) {
  const skills = (index?.skills || []).map((skill) => {
    const skillPath = path.resolve(root, String(skill.path || ""));
    const relative = path.relative(root, skillPath);
    if (path.basename(skillPath) !== "SKILL.md" || relative.startsWith("..") || path.isAbsolute(relative)) {
      throw new Error(`invalid installed skill path: ${skill.name}`);
    }
    if (typeof skill.tree_hash !== "string" || !/^[0-9a-f]{64}$/.test(skill.tree_hash)) {
      throw new Error(`installed skill has no whole-tree hash: ${skill.name}`);
    }
    const liveHash = verifyTrees ? hashSkillTree(path.dirname(skillPath)) : skill.tree_hash;
    if (verifyTrees && skill.tree_hash !== liveHash) {
      throw new Error(`installed skill snapshot changed: ${skill.name}`);
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
  }).sort((left, right) => compareCodePoints(left[0], right[0]));
  const selection = index?.selection || {};
  const normalized = {
    profiles: [...(selection.profiles || [])].sort(compareCodePoints),
    skill_profiles: [...(selection.skill_profiles || [])].sort(compareCodePoints),
    explicit_skills: [...(selection.explicit_skills || [])].sort(compareCodePoints),
    ...(Object.hasOwn(selection, "external_exposure_skills")
      ? { external_exposure_skills: [...selection.external_exposure_skills].sort(compareCodePoints) }
      : {}),
    ...(Object.hasOwn(selection, "profile_selection")
      ? { profile_selection: selection.profile_selection }
      : {}),
    required_review: Object.fromEntries(
      Object.entries(selection.required_review || {})
        .sort(([left], [right]) => compareCodePoints(left, right))
        .map(([profile, names]) => [profile, [...names].sort(compareCodePoints)]),
    ),
    conditional_skills: selection.conditional_skills || {},
    profile_routing: selection.profile_routing || {},
    skills,
  };
  return crypto.createHash("sha256").update(JSON.stringify(normalized)).digest("hex");
}

function normalizedRoutingHashStrings(skill, key) {
  const value = skill[key] ?? [];
  if (!Array.isArray(value) || value.some((entry) => typeof entry !== "string")) {
    throw new Error(`installed skill has invalid ${key}: ${skill.name}`);
  }
  return [...value].sort(compareCodePoints);
}

function sha256Bytes(content) {
  return crypto.createHash("sha256").update(content).digest("hex");
}

function compareCodePoints(left, right) {
  const first = Array.from(String(left), (character) => character.codePointAt(0));
  const second = Array.from(String(right), (character) => character.codePointAt(0));
  for (let index = 0; index < Math.min(first.length, second.length); index += 1) {
    if (first[index] !== second[index]) return first[index] - second[index];
  }
  return first.length - second.length;
}

function treeIntegrity(root) {
  const entries = [];
  const visit = (current, relative) => {
    const stat = fs.lstatSync(current);
    if (stat.isSymbolicLink() || !stat.isDirectory()) {
      throw new Error(`skill tree integrity root is unsafe: ${current}`);
    }
    entries.push({ path: relative, type: "directory", mode: stat.mode & 0o777 });
    for (const name of fs.readdirSync(current).sort()) {
      const child = path.join(current, name);
      const childRelative = relative ? `${relative}/${name}` : name;
      const childStat = fs.lstatSync(child);
      if (childStat.isSymbolicLink()) {
        throw new Error(`skill tree integrity contains a symlink: ${child}`);
      }
      if (childStat.isDirectory()) {
        visit(child, childRelative);
      } else if (childStat.isFile()) {
        entries.push({
          path: childRelative,
          type: "file",
          mode: childStat.mode & 0o777,
          sha256: crypto.createHash("sha256").update(fs.readFileSync(child)).digest("hex"),
        });
      } else {
        throw new Error(`skill tree integrity contains a special file: ${child}`);
      }
    }
  };
  visit(root, "");
  entries.sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
  return crypto.createHash("sha256")
    .update(JSON.stringify({ version: 1, entries }))
    .digest("hex");
}

function skillLinksCommitment(skillPlanHash, links) {
  if (typeof skillPlanHash !== "string" || !/^[0-9a-f]{64}$/.test(skillPlanHash)) {
    throw new Error("skill link commitment has an invalid skill plan hash");
  }
  const owned = (links || []).filter((link) => [
    "linked",
    "copied",
    "removed-stale-linked",
    "removed-stale-copied",
  ].includes(link?.status));
  const rows = owned.map((link) => [
    link.name,
    link.host,
    String(link.path).replaceAll("\\", "/"),
    link.status,
    link.tree_integrity ?? null,
  ]).sort((left, right) => compareCodePoints(JSON.stringify(left), JSON.stringify(right)));
  return crypto.createHash("sha256").update(JSON.stringify({
    version: SKILL_LINKS_COMMITMENT_VERSION,
    skill_plan_hash: skillPlanHash,
    links: rows,
  })).digest("hex");
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
      excludes: metadata.excludes,
      tags: metadata.tags,
      description: metadata.description,
      trigger: metadata.trigger,
      triggers: metadata.triggers,
      activation: metadata.activation,
      activationDeclared: metadata.activationDeclared,
      taskTerms: metadata.taskTerms,
      pathGlobs: metadata.pathGlobs,
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
    activation: ["always", "conditional", "on-demand"].includes(String(metadata.activation))
      ? String(metadata.activation)
      : "on-demand",
    activationDeclared: typeof metadata.activation === "string" && metadata.activation.length > 0,
    taskTerms: arrayValue(metadata.taskTerms),
    pathGlobs: arrayValue(metadata.pathGlobs),
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

function removeStaleProjectSkillLinks(root, skills, previousIndex, force = false, transaction = null) {
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
    if (link.status === "linked") {
      if (!stat.isSymbolicLink() || link.filesystem_kind !== "symlink" || typeof link.target !== "string") {
        removed.push({ name: link.name, host: link.host, path: link.path, status: "preserved-kind-mismatch" });
        continue;
      }
      if (fs.readlinkSync(target) !== link.target) {
        removed.push({ name: link.name, host: link.host, path: link.path, status: "preserved-target-mismatch" });
        continue;
      }
      withHostPathMutation(
        transaction,
        target,
        [{ kind: "absent" }],
        () => fs.unlinkSync(target),
      );
      removed.push({ name: link.name, host: link.host, path: link.path, status: "removed-stale-linked" });
      continue;
    }
    if (link.status !== "copied" || link.filesystem_kind !== "directory" || !stat.isDirectory()) {
      removed.push({ name: link.name, host: link.host, path: link.path, status: "preserved-unverified-ownership" });
      continue;
    }
    if (typeof link.tree_hash !== "string" || hashSkillTree(target) !== link.tree_hash) {
      removed.push({ name: link.name, host: link.host, path: link.path, status: "preserved-integrity-mismatch" });
      continue;
    }
    withHostPathMutation(
      transaction,
      target,
      [{ kind: "absent" }],
      () => fs.rmSync(target, { recursive: true, force: true }),
    );
    removed.push({ name: link.name, host: link.host, path: link.path, status: "removed-stale-copied" });
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

function linkProjectSkill(root, skill, host, previousIndex, force = false, transaction = null) {
  const srcDir = path.dirname(path.join(root, skill.path));
  const hostRoot = hostSkillRoot(root, host);
  if (pathHasSymlink(root, hostRoot)) {
    return { name: skill.name, host, path: path.relative(root, hostRoot), status: "skipped-host-root-symlink" };
  }
  const destDir = path.join(hostRoot, skill.name);
  ensureChildPath(hostRoot, destDir);
  const destSkill = path.join(destDir, "SKILL.md");
  const previousLink = previousIndex?.links?.find((link) => (
    link?.name === skill.name && link?.host === host && path.resolve(root, link.path) === destDir
  ));
  let replaceExisting = false;
  if (fs.existsSync(destDir)) {
    const stat = fs.lstatSync(destDir);
    if (stat.isSymbolicLink()) {
      if (
        previousLink?.status !== "linked"
        || previousLink.filesystem_kind !== "symlink"
        || typeof previousLink.target !== "string"
        || fs.readlinkSync(destDir) !== previousLink.target
      ) {
        return { name: skill.name, host, path: path.relative(root, destDir), status: "skipped-unverified-existing" };
      }
      replaceExisting = true;
    } else if (fs.existsSync(destSkill)) {
      if (previousLink?.status === "linked") {
        return { name: skill.name, host, path: path.relative(root, destDir), status: "skipped-kind-mismatch" };
      }
      if (
        previousLink?.status !== "copied"
        || previousLink.filesystem_kind !== "directory"
        || typeof previousLink.tree_hash !== "string"
      ) {
        return { name: skill.name, host, path: path.relative(root, destDir), status: "skipped-unverified-existing" };
      }
      if (hashSkillTree(destDir) !== previousLink.tree_hash) {
        return { name: skill.name, host, path: path.relative(root, destDir), status: "skipped-user-modified" };
      }
      replaceExisting = true;
    } else if (force) {
      replaceExisting = true;
    } else {
      return { name: skill.name, host, path: path.relative(root, destDir), status: "skipped-existing" };
    }
  }
  fs.mkdirSync(path.dirname(destDir), { recursive: true });
  const relTarget = path.relative(path.dirname(destDir), srcDir);
  return withHostPathMutation(
    transaction,
    destDir,
    [
      { kind: "absent" },
      { kind: "symlink", target: relTarget },
      { kind: "directory", tree_hash: skill.tree_hash },
    ],
    () => {
      if (replaceExisting) fs.rmSync(destDir, { recursive: true, force: true });
      try {
        if (process.env.AGENT_FLOW_TEST_FORCE_COPY_HOST_SKILLS === "1") {
          throw new Error("injected host skill copy fallback");
        }
        fs.symlinkSync(relTarget, destDir, "dir");
        return {
          name: skill.name,
          host,
          path: path.relative(root, destDir),
          status: "linked",
          filesystem_kind: "symlink",
          target: relTarget,
          tree_hash: skill.tree_hash,
          tree_integrity: treeIntegrity(srcDir),
        };
      } catch {
        copyBundledDirIfMissingOrSame(srcDir, destDir, true);
        return {
          name: skill.name,
          host,
          path: path.relative(root, destDir),
          status: "copied",
          filesystem_kind: "directory",
          tree_hash: skill.tree_hash,
          tree_integrity: treeIntegrity(srcDir),
        };
      }
    },
  );
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
  throw new Error("no Python with PyYAML available for workflow export");
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

function assertCompletionMarkers(phase, artifact, root) {
  const content = fs.readFileSync(artifact, "utf8");
  const missing = missingMarkersForPhase(content, phase, root);
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
const PROJECT_LOCAL_SKILL_INCLUDE_TERMS = [
  "code development",
  "code generation",
  "code review",
  "development or review",
  "developing or reviewing",
  "implementing or reviewing",
  "writing or reviewing",
  "modifying or reviewing",
  "architecture review",
  "android code",
  "kotlin implementation",
  "compose implementation",
  "코드 개발",
  "코드 작성",
  "코드 수정",
  "코드 리뷰",
  "코드리뷰",
  "구현·리뷰",
  "개발/수정/리뷰",
  "작성·리뷰",
];
const PROJECT_LOCAL_SKILL_EXCLUDE_TERMS = [
  "figma",
  "screen-spec",
  "screen spec",
  "design link",
  "figma.com/design",
  "git commit",
  "git push",
  "pull request",
  "pull-request",
  "pr-review",
  "pr review",
  "branch-pr",
  "branch base",
  "branch creation",
  "branch review",
  "release branch",
  "worktree",
  "cleanup",
  "merge cleanup",
  "merge review",
  "release-first",
  "pretooluse",
  "posttooluse",
  "guard-worktree",
  "guard-protected-branch",
  "comment-checker",
  "claude hook",
  "codex hook",
  "agent-flow lifecycle",
  "workflow lifecycle",
];
const PROJECT_LOCAL_SKILL_EXCLUDE_TOKEN_PATTERN = /(^|[^a-z0-9])(pr|branch|merge)([^a-z0-9]|$)/;

function missingMarkersForPhase(content, phase, root) {
  const missing = missingMarkers(content, phase.required_markers ?? []);
  missing.push(...missingProjectLocalSkillMarkers(content, root, phase.id));
  return missing;
}

function localSkillPromptBlock(root, phaseId) {
  const docs = applicableProjectLocalSkillDocs(root, phaseId);
  if (docs.length === 0) {
    return "";
  }
  return [
    "",
    "",
    "## Project-local code/review skills",
    "",
    "Project-local markdown skill docs that apply to code generation or code review were found.",
    "Read only the applicable docs before completing this phase. Design/Figma, hook, branch, PR, merge, and cleanup skills are intentionally excluded here.",
    "",
    "Applicable docs:",
    "",
    ...docs.map((doc) => localSkillPromptLine(root, doc)),
    "",
    "When this block appears, the `## Completion Gate` must include:",
    "",
    "```text",
    "project-local-skills: checked",
    "project-local-skills-used: <comma-separated applicable skill names>",
    PROJECT_LOCAL_SKILL_APPLIED_MARKER,
    "```",
    "",
    "If this block is absent, `project-local-skills: n/a` remains valid.",
    "",
  ].join("\n");
}

function localSkillPromptLine(root, doc) {
  const absolutePath = path.isAbsolute(doc.path)
    ? doc.path
    : path.join(root, doc.path);
  return `- \`${doc.path}\` (\`${doc.name}\`) — \`${absolutePath}\``;
}

function missingProjectLocalSkillMarkers(content, root, phaseId) {
  const docs = applicableProjectLocalSkillDocs(root, phaseId);
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

function applicableProjectLocalSkillDocs(root, phaseId) {
  if (!CODE_REVIEW_LOCAL_SKILL_PHASES.has(phaseId)) {
    return [];
  }
  return projectLocalSkillDocs(root)
    .filter((doc) => isCodeReviewLocalSkill(doc))
    .sort((a, b) => a.name.localeCompare(b.name));
}

function projectLocalSkillDocs(root) {
  const indexDocs = localSkillDocsFromIndex(root);
  if (indexDocs.length > 0) {
    return dedupeLocalSkillDocs(indexDocs);
  }
  return dedupeLocalSkillDocs(localSkillDocsFromTree(root));
}

function localSkillDocsFromIndex(root) {
  const indexPath = path.join(root, ".agent-flow", "skills", "index.json");
  if (!fs.existsSync(indexPath)) {
    return [];
  }
  let payload;
  try {
    payload = JSON.parse(fs.readFileSync(indexPath, "utf8"));
  } catch (_error) {
    return [];
  }
  if (!Array.isArray(payload.skills)) {
    return [];
  }
  return payload.skills
    .filter((skill) => skill && ["local", "project"].includes(skill.source))
    .filter((skill) => isProjectLocalSkillPath(String(skill.path ?? "")))
    .map((skill) => ({
      name: String(skill.name || path.basename(path.dirname(String(skill.path ?? "")))),
      path: String(skill.path ?? ""),
      description: [
        skill.description,
        skill.trigger,
        ...(Array.isArray(skill.tags) ? skill.tags : []),
        ...(Array.isArray(skill.workflowPhases) ? skill.workflowPhases : []),
        ...(Array.isArray(skill.reviewAngles) ? skill.reviewAngles : []),
      ].filter(Boolean).join(" "),
    }));
}

function localSkillDocsFromTree(root) {
  const base = path.join(root, ".agent-flow", "local-skills");
  if (!fs.existsSync(base)) {
    return [];
  }
  return fs.readdirSync(base, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => {
      const skillPath = path.join(base, entry.name, "SKILL.md");
      if (!fs.existsSync(skillPath)) {
        return null;
      }
      return {
        name: entry.name,
        path: path.relative(root, skillPath).split(path.sep).join("/"),
        description: localSkillMetadataText(skillPath),
      };
    })
    .filter(Boolean);
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
  return (
    normalized.endsWith("/SKILL.md") &&
    (normalized.startsWith(".agent-flow/local-skills/") || normalized.startsWith("skills/"))
  );
}

function isCodeReviewLocalSkill(doc) {
  const haystack = `${doc.name} ${doc.path} ${doc.description}`.toLowerCase();
  if (
    PROJECT_LOCAL_SKILL_EXCLUDE_TERMS.some((term) => haystack.includes(term)) ||
    PROJECT_LOCAL_SKILL_EXCLUDE_TOKEN_PATTERN.test(haystack)
  ) {
    return false;
  }
  return PROJECT_LOCAL_SKILL_INCLUDE_TERMS.some((term) => haystack.includes(term));
}

function dedupeLocalSkillDocs(docs) {
  const byName = new Map();
  for (const doc of docs) {
    if (!byName.has(doc.name)) {
      byName.set(doc.name, doc);
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

function nextPhaseIndex(state, phases, phase, artifact) {
  if (!phase.routes) {
    return state.phase_index + 1;
  }
  const key = nodeRouteKey(phase, artifact);
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
    if (verdict === "approve" || verdict === "request-changes") {
      if (verdict === "approve" && phase.routes?.["request-changes"] && artifactHasFailureMarkers(artifact)) {
        return "request-changes";
      }
      return verdict;
    }
    throw new Error("blocked: multi-review artifact must include verdict: approve or verdict: request-changes");
  }
  if (phase.id === "pr-watch") {
    const status = readArtifactStatus(artifact);
    if (["green", "merged", "skipped", "comments", "has_comments", "ci-failed", "ci_failed", "pending", "closed", "error"].includes(status)) {
      return status;
    }
    return "default";
  }
  if (phase.id === "plan-review" || phase.id === "architecture-review" || phase.id === "merge-approval") {
    const verdict = readArtifactVerdict(artifact);
    if (["approve", "request-changes", "blocked"].includes(verdict)) {
      if (verdict === "approve" && phase.routes?.["request-changes"] && artifactHasFailureMarkers(artifact)) {
        return "request-changes";
      }
      return verdict;
    }
    return "default";
  }
  return readArtifactStatus(artifact) ?? readArtifactVerdict(artifact) ?? "default";
}

function phaseIndex(phases, id) {
  const index = phases.findIndex((phase) => phase.id === id);
  if (index === -1) {
    throw new Error(`unknown phase: ${id}`);
  }
  return index;
}

function readArtifactStatus(pathName) {
  const content = fs.readFileSync(pathName, "utf8");
  const match = content.match(/^status:\s*([a-z_-]+)\s*$/im);
  return match?.[1]?.toLowerCase();
}

function readArtifactVerdict(pathName) {
  const content = fs.readFileSync(pathName, "utf8");
  const match = content.match(/^verdict:\s*([a-z-]+)\s*$/im);
  return match?.[1]?.toLowerCase();
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
  const overall = readMultiReviewOverallVerdict(content);
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

function readMultiReviewOverallVerdict(content) {
  // Python runner와 같은 heading alias(Overall/Final [Verdict])를 인정한다.
  const sections = content.split(/^##[ \t]+(?:Overall|Final)(?:[ \t]+Verdict)?[ \t]*$/im);
  if (sections.length < 2) {
    return undefined;
  }
  if (sections.length > 2) {
    return "invalid-verdict";
  }
  const overallBlock = sections[sections.length - 1].split(/^#{1,6}[ \t]+/m, 1)[0] ?? "";
  const verdicts = [...overallBlock.matchAll(/^verdict:\s*([a-z-]+)\s*$/gim)]
    .map((match) => match[1]);
  if (verdicts.length === 0) {
    return undefined;
  }
  if (verdicts.length !== 1) {
    return "invalid-verdict";
  }
  return verdicts[0];
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
    stateFor(reviewerId).verdict = match[2];
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
    const verdict = reviewerBlock.match(/^\s*verdict:\s*(approve|request-changes)\s*$/im)?.[1];
    if (verdict && !["approve", "request-changes"].includes(verdict)) {
      continue;
    }
    if (verdict) {
      stateFor(reviewerId).verdict = verdict;
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
    if (typeof data.passed !== "boolean" || !Array.isArray(data.results) || data.results.length === 0) {
      if (typeof data.passed === "boolean" && typeof data.status === "string") {
        const status = data.status.trim().toLowerCase().replace(/_/g, "-");
        if (data.passed === false && ["request-changes", "blocked", "error", "pending"].includes(status)) {
          return status;
        }
      }
      return typeof data.passed === "boolean" && data.passed === false ? "request-changes" : "default";
    }
    // 완료 보고는 실제 실행한 gate command와 결과 evidence가 함께 있을 때만 허용한다.
    const requiredResults = data.results.filter((r) => r && r.required !== false);
    const resultsPass =
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

function canonicalAgentFlowBlock() {
  const sourcePath = path.join(KIT_ROOT, "bootstrap", "agent-flow.md");
  const block = fs.readFileSync(sourcePath, "utf8");
  const start = "<!-- agent-flow:start -->";
  const end = "<!-- agent-flow:end -->";
  if (
    countOccurrences(block, start) !== 1
    || countOccurrences(block, end) !== 1
    || block.indexOf(start) > block.indexOf(end)
  ) {
    throw new Error(`invalid canonical agent-flow block: ${sourcePath}`);
  }
  return block;
}

function syncProjectAgentDocuments(root, canonicalBlock = canonicalAgentFlowBlock()) {
  const paths = [path.join(root, "AGENTS.md"), path.join(root, "CLAUDE.md")];
  const planned = paths.map((pathName) => ({
    pathName,
    content: planBootstrapBlockUpsert(pathName, canonicalBlock),
  }));
  for (const entry of planned) {
    writeManagedFile(entry.pathName, entry.content);
  }
}

function planBootstrapBlockUpsert(pathName, canonicalBlock) {
  const start = "<!-- agent-flow:start -->";
  const end = "<!-- agent-flow:end -->";
  const current = fs.existsSync(pathName) ? fs.readFileSync(pathName, "utf8") : "";
  const startCount = countOccurrences(current, start);
  const endCount = countOccurrences(current, end);
  if (startCount !== endCount || startCount > 1) {
    throw new Error(`invalid agent-flow markers: ${pathName}`);
  }
  if (startCount === 1 && current.indexOf(start) > current.indexOf(end)) {
    throw new Error(`invalid agent-flow marker order: ${pathName}`);
  }
  const newline = current.includes("\r\n") ? "\r\n" : "\n";
  const block = canonicalBlock.replace(/\r?\n/g, newline).replace(/(?:\r?\n)+$/, "");
  if (startCount === 1) {
    const before = current.slice(0, current.indexOf(start));
    const after = current.slice(current.indexOf(end) + end.length);
    return `${before}${block}${after}`;
  }
  if (!current) {
    return `${block}${newline}`;
  }
  const separator = current.endsWith(`${newline}${newline}`)
    ? ""
    : current.endsWith(newline) ? newline : `${newline}${newline}`;
  const finalNewline = current.endsWith(newline) ? newline : "";
  return `${current}${separator}${block}${finalNewline}`;
}

function countOccurrences(text, marker) {
  return text.split(marker).length - 1;
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
  writeManagedFile(pathName, next);
}

function removeGitignoreEntries(pathName, entries) {
  if (!fs.existsSync(pathName)) return;
  const removals = new Set(entries);
  const current = fs.readFileSync(pathName, "utf8");
  const lines = current.split(/\r?\n/);
  const filtered = lines.filter((line) => !removals.has(line.trim()));
  if (filtered.length === lines.length) return;
  const next = `${filtered.join("\n").replace(/\n*$/, "")}\n`;
  writeManagedFile(pathName, next);
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

function bootstrapMarkdown(label, canonicalBlock = canonicalAgentFlowBlock()) {
  return `# ${label} Agent Flow Bootstrap\n\n${canonicalBlock}`;
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

function pushWatchPromptMarkdown() {
  return `# push-watch\n\nCommit, push, open a PR, and start the PR watch loop.\n\nUse \`${AGENT_FLOW_COMMAND} run push-watch\`.\n\nDo not run on protected branches. Do not merge without explicit approval.\n`;
}

function pushWatchTickPromptMarkdown() {
  return `# push-watch-tick\n\nPoll the current PR checks and review threads.\n\nUse \`${AGENT_FLOW_COMMAND} run push-watch-tick\`.\n\nWrite \`artifacts/pr-watch.md\` with one status line: \`status: green\`, \`status: comments\`, \`status: ci-failed\`, or \`status: pending\`.\n`;
}

function phasePrompt(phase, root = null) {
  const markers = phase.required_markers?.length
    ? `\n\n## Completion markers\n\nThe runner blocks this phase until the artifact includes a \`## Completion Gate\` section with these marker lines:\n\n${phase.required_markers.map((marker) => `- \`${marker}\``).join("\n")}\n`
    : "";
  const localSkillBlock = root ? localSkillPromptBlock(root, phase.id) : "";
  return `# ${phase.id}\n\n${phase.instruction}${markers}${localSkillBlock}\n\nSave the required artifact before running:\n\n\`\`\`bash\n${AGENT_FLOW_COMMAND} run advance\n\`\`\`\n`;
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
  const decoded = Buffer.from(match[1], "base64");
  if (decoded.toString("base64") !== match[1]) return null;
  const scriptPath = decoded.toString("utf8");
  if (!Buffer.from(scriptPath, "utf8").equals(decoded)) return null;
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
  if (verifier && MANAGED_HOOK_SCRIPT_NAMES.includes(path.basename(verifier.scriptPath))) {
    return path.basename(verifier.scriptPath);
  }
  for (const match of command.matchAll(/(?:^|[\s'"])([A-Za-z0-9+/]+={0,2})(?=$|[\s'"])/g)) {
    const decoded = Buffer.from(match[1], "base64");
    if (decoded.toString("base64") === match[1]) {
      const decodedPath = decoded.toString("utf8");
      if (Buffer.from(decodedPath, "utf8").equals(decoded) && MANAGED_HOOK_SCRIPT_NAMES.includes(path.basename(decodedPath))) {
        return path.basename(decodedPath);
      }
    }
  }
  if (
    command.includes("AGENT_FLOW_MANAGED_HOOK_SOURCE_PATH")
    && command.includes("agent-flow-managed-hook-")
    && command.includes("descriptor execution unavailable")
  ) return "__managed-verifier__";
  const normalized = unquoteShellWord(command).replaceAll("\\", "/").replaceAll("'", "").replaceAll('"', "");
  for (const scriptName of MANAGED_HOOK_SCRIPT_NAMES) {
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
  for (const scriptName of MANAGED_HOOK_SCRIPT_NAMES) {
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
    writeManagedFile(settingsPath, `${JSON.stringify(settings, null, 2)}\n`);
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
  mergeHookSettings(settings, claudeHooksSettings(root).hooks);
  writeManagedFile(settingsPath, `${JSON.stringify(settings, null, 2)}\n`);
}

function ompHooksExtensionSource() {
  return String.raw`import fs from "node:fs";
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const HOOK_DIR = path.join(ROOT, ".agent-flow", "scripts", "hooks");
const WRITE_TOOL_RE = new RegExp(${JSON.stringify(WRITE_TOOL_MATCHER)}, "i");

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
    if (WRITE_TOOL_RE.test(String(event?.toolName || "")) || isBashTool(event?.toolName)) {
      const result = await runHook("guard-worktree-write.py", hookPayload(event, ctx), ctx);
      if (result.block) {
        return { block: true, reason: result.reason };
      }
    }
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
  return writeManagedFileIfMissingOrSame(
    path.join(root, ".omp", "extensions", "agent-flow-hooks.ts"),
    ompHooksExtensionSource(),
    forceManaged,
  );
}

function makeHooksExecutable(root) {
  const hooksDir = path.join(root, ".agent-flow", "scripts", "hooks");
  if (!fs.existsSync(hooksDir)) {
    return;
  }
  withManagedInstallMutation(hooksDir, () => {
    for (const entry of fs.readdirSync(hooksDir)) {
      if (entry.endsWith(".sh") || entry === "comment-checker.py" || entry === "guard-worktree-write.py") {
        fs.chmodSync(path.join(hooksDir, entry), 0o755);
      }
    }
  });
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
  runPythonCliCommand("architecture-lint", args);
}

function runGates(args) {
  runPythonCliCommand("gates", args);
}

function runPythonCliCommand(subcommand, args) {
  const root = resolveAgentFlowRoot(process.cwd());
  const pythonPathEntries = [
    path.join(KIT_ROOT, "src"),
    root ? installedPythonRuntimePath(root) : "",
    process.env.PYTHONPATH,
  ].filter(Boolean);
  const env = {
    ...process.env,
    PYTHONPATH: [...new Set(pythonPathEntries)].join(path.delimiter),
  };
  const result = safeSpawnSync(
    "python3",
    ["-m", "agent_flow.cli", subcommand, ...args],
    {
      cwd: process.cwd(),
      env,
      encoding: "utf8",
      stdio: ["ignore", "inherit", "inherit"],
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

  if (command === "sync") {
    syncProject();
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

  console.error("usage: agent-flow-kit install [--force-managed] | sync | gates [--profile <id>] [--worktree <name>] | architecture-lint [--profile <id>] [--files ...] | run <install|start|status|next|advance|push-watch|push-watch-tick>");
  process.exit(1);
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
}
