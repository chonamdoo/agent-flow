#!/usr/bin/env node

import fs from "node:fs";
import crypto from "node:crypto";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { SKILL_DEPENDENCIES, mergeInstallSelectionWithPrevious, resolveInstallSelection } from "../lib/skill-selection.mjs";

const command = process.argv[2];
const AGENT_FLOW_COMMAND = "agent-flow";
const HOME = process.env.HOME || process.env.USERPROFILE || "";
const KIT_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const RUNTIME_PYTHON_RELATIVE = path.join(".agent-flow", "runtime", "python");
const installArgs = process.argv.slice(3);
const forceManaged = installArgs.includes("--force-managed");
// 이 표식이 남아 있으면 OMP 확장 파일을 kit 소유로 보고 업그레이드한다.
const OMP_EXTENSION_MARKER = "agent-flow: managed omp extension";
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
  const previousSkillIndex = readJsonIfExists(path.join(agentFlowDir, "skills", "index.json"));
  installSelection = mergeInstallSelectionWithPrevious(installSelection, previousSkillIndex, KIT_ROOT, root);
  const phases = fullFeaturePhases();

  for (const name of ["runs", "state", "handoffs", "team", "worktrees", "workflows", "skills", "templates", "prompts", "rules", "bootstrap"]) {
    fs.mkdirSync(path.join(agentFlowDir, name), { recursive: true });
  }
  fs.mkdirSync(path.join(agentFlowDir, "local-skills"), { recursive: true });

  fs.mkdirSync(path.join(agentFlowDir, "skills", "agent-flow"), { recursive: true });
  fs.mkdirSync(path.join(agentFlowDir, "skills", "full-feature-workflow"), { recursive: true });

  const payload = {
    install_scope: "project",
    profile,
    profiles: installSelection.profiles,
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
    new Set(["index.json", ...GENERATED_PROJECT_SKILL_NAMES]),
    installSelection.copyRootNames,
  );
  // kit이 배포하는 profile은 갱신한다. 사용자 편집을 보호한다고 두면 새 kit이
  // 추가한 필드(skill_sources 등)가 기존 설치본에 영영 안 닿는다. 다만 지우지는
  // 않는다 — prune을 켜면 사용자가 만든 custom profile이 함께 날아간다.
  // 덮기 전에는 사본을 남긴다.
  upgradeBundledProfiles(root, path.join(KIT_ROOT, "profiles"), path.join(agentFlowDir, "profiles"));
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, "templates"), path.join(agentFlowDir, "templates"), forceManaged, new Set(), true, forceManaged);
  const skillIndex = installProjectSkills(root, agentFlowDir, previousSkillIndex, forceManaged, installSelection);
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, "scripts"), path.join(agentFlowDir, "scripts"), forceManaged);
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
    "scripts/check-context-docs.*",
    "agent-flow/",
  ]);
  removeGitignoreEntries(gitignorePath, [
    "graphify/",
    "graphify-out/manifest.json",
    "graphify-out/cost.json",
  ]);
  removeLegacyProjectSkillCopies(root, "graphify");
  upsertBootstrapBlock(path.join(root, "AGENTS.md"), "AGENTS.md", root);
  upsertBootstrapBlock(path.join(root, "CLAUDE.md"), "CLAUDE.md", root);
  pruneRetiredHookScripts(root);
  makeHooksExecutable(root);
  installClaudeHooks(root);
  installOmpHooks(root);

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

function runWorkflowCommand(args) {
  const subcommand = args[0];
  const root = resolveAgentFlowRoot(process.cwd());
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
    };
    writeJson(path.join(runDir, "manifest.json"), state);
    writeJson(currentRunPath(root), state);
    printNext(state, root);
    return;
  }

  if (subcommand === "status") {
    const state = readCurrentRun(root);
    printStatus(state, root);
    return;
  }

  if (subcommand === "next") {
    printNext(readCurrentRun(root), root);
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

  if (subcommand === "advance") {
    const state = readCurrentRun(root);
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
    assertCompletionMarkers(phase, artifact, root, state.workflow);
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
  const localSkillBlock = root ? localSkillPromptBlock(root, phase.id, state.workflow) : "";
  console.log(`Current phase: ${phase.id}`);
  console.log(`Run: ${state.run_id}`);
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
      state.workflow,
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
    reason,
    required_artifact: requiredArtifact,
    next_command: nextCommand,
  };
  console.log(`${state.workflow} ${state.run_id} ${status} phase=${phase?.id ?? "-"}`);
  console.log(`status: ${statusValue(status)}`);
  console.log(`run: ${statusValue(payload.run)}`);
  console.log(`task: ${statusValue(payload.task)}`);
  console.log(`current_phase: ${statusValue(payload.current_phase)}`);
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
      copyBundledDirIfMissingOrSame(srcPath, destPath, force, excludedRootDirs, false, pruneExtraneous, preservedExtraneousRootNames, null);
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
      if (!sourceNames.has(entry.name) && !(isRoot && preservedExtraneousRootNames.has(entry.name))) {
        fs.rmSync(path.join(dest, entry.name), { recursive: true, force: true });
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
  fs.rmSync(dest, { recursive: true, force: true });
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
  if (fs.existsSync(destDir)) {
    const stat = fs.lstatSync(destDir);
    if (stat.isSymbolicLink()) {
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

function assertCompletionMarkers(phase, artifact, root, workflow) {
  const content = fs.readFileSync(artifact, "utf8");
  const missing = missingMarkersForPhase(content, phase, root, workflow);
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

function missingMarkersForPhase(content, phase, root, workflow) {
  const missing = missingMarkers(content, phase.required_markers ?? []);
  if (!root) {
    return missing;
  }
  return missing.concat(missingProjectLocalSkillMarkers(content, root, phase, workflow));
}


function localSkillPromptBlock(root, phase, workflow) {
  const phaseId = typeof phase === "string" ? phase : phase?.id;
  if (!root || !phaseId) {
    return "";
  }
  const out = runSkillsCli(["prompt", "--root", root, "--phase", phaseId, "--workflow", workflow || "default"]);
  return out ?? "";
}

function missingProjectLocalSkillMarkers(content, root, phase, workflow) {
  const phaseId = typeof phase === "string" ? phase : phase?.id;
  if (!root || !phaseId) {
    return [];
  }
  const artifact = writeTempArtifact(content);
  try {
    // task/changed_files/since는 넘기지 않는다. Python이 활성 run에서 직접 도출해야
    // JS·status·Python runner 세 경로가 같은 skill 집합을 본다.
    const out = runSkillsCli([
      "markers",
      "--root",
      root,
      "--phase",
      phaseId,
      "--workflow",
      workflow || "default",
      "--artifact",
      artifact,
    ]);
    if (out === null) {
      // resolver가 대답하지 못하면 skill 강제가 있는지 없는지 알 수 없다.
      // 조용히 통과시키면 강제 전체가 무음으로 꺼진다 — fail-closed로 막는다.
      return ["skill-check-unavailable: python skill resolver did not answer"];
    }
    const parsed = JSON.parse(out);
    return Array.isArray(parsed) ? parsed : [];
  } catch (error) {
    process.stderr.write(`agent-flow: skill marker check failed: ${error.message}\n`);
    return ["skill-check-unavailable: python skill resolver did not answer"];
  } finally {
    try {
      fs.rmSync(artifact, { force: true });
    } catch (_error) {
      /* 임시 파일 정리 실패가 phase 판정을 막아서는 안 된다. */
    }
  }
}

function writeTempArtifact(content) {
  const file = path.join(os.tmpdir(), `agent-flow-markers-${process.pid}-${Date.now()}.md`);
  fs.writeFileSync(file, content, "utf8");
  return file;
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
- 프로젝트 skill은 \`skills/<name>/SKILL.md\` 또는 private \`.agent-flow/local-skills/<name>/SKILL.md\`에 둔다.
- install/bootstrap 후 \`.agent-flow/skills/index.json\` metadata를 보고 필요한 skill만 읽는다. 모든 SKILL.md 전문을 항상 읽지 않는다.
- Claude/Codex/OMP 프로젝트 skill 경로는 leader checkout의 install 결과를 따른다. worktree 안에서 install, index 재생성, skill link 재생성을 하지 않는다.
- Claude/Codex/OMP hook이 자동 차단하는 보호 브랜치 commit/push와 leader checkout/switch 금지는 모든 host에서 동일하게 지킨다.

During code generation, modification, and code review phases, apply \`code-generation-discipline\`. Resolve required skills from active profile metadata, installed skill index, changed files, and task scope. Load only the touched profile skill union.
For Android/Kotlin/Compose/KMP changes, read matching local skill files from the Android profile's \`android_skills\` and \`chrisbanes_skills\` for the active host. React Native \`android/\` native changes also apply the Android profile mapping. Do not require unrelated platform skills, and do not install, copy, link, or load duplicate skills from other host directories. If a required local skill is missing, report \`missing local <group>: <skill>\` with the profile source URL and ask the user to install it.
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

function phasePrompt(phase) {
  const markers = phase.required_markers?.length
    ? `\n\n## Completion markers\n\nThe runner blocks this phase until the artifact includes a \`## Completion Gate\` section with these marker lines:\n\n${phase.required_markers.map((marker) => `- \`${marker}\``).join("\n")}\n`
    : "";
  // skill 목록은 여기에 굽지 않는다. install 시점 스냅샷은 새 skill이 설치되면 바로 stale해진다.
  // 실제 목록은 `agent-flow-kit run next` / `status`가 매번 resolver로 다시 만든다.
  const skillNote = "\n\n## Required skills\n\nRun `agent-flow-kit run next` for the resolved skill list for this phase — it is computed live, not baked here.\n";
  return `# ${phase.id}\n\n${phase.instruction}${markers}${skillNote}\n\nSave the required artifact before running:\n\n\`\`\`bash\n${AGENT_FLOW_COMMAND} run advance\n\`\`\`\n`;
}

function shellQuote(value) {
  return `'${String(value).replaceAll("'", "'\\''")}'`;
}

function hookScriptCommand(root, scriptName) {
  return shellQuote(path.join(root, ".agent-flow", "scripts", "hooks", scriptName));
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

// Read 계열 tool 이름은 host마다 다르다. comment-checker의 write matcher와 같은 방식으로 합집합을 쓴다.
const READ_TOOL_MATCHER = "^(Read|read|read_file|view|cat)$";

// 관리 대상에서 빠진 hook은 기존 설치본 settings에 그대로 남는다. mergeHookSettings는
// additive-only라서, 스크립트 파일만 지우면 host가 없는 경로를 계속 실행해 셸이 통째로
// 막힌다. 은퇴한 이름을 여기 남겨 install 때 등록에서 걷어낸다.
const RETIRED_MANAGED_HOOK_SCRIPTS = ["guard-worktree.sh", "guard-worktree-write.py"];

function pruneRetiredHooks(settings) {
  if (!settings || typeof settings !== "object" || !settings.hooks) {
    return false;
  }
  let changed = false;
  for (const [event, entries] of Object.entries(settings.hooks)) {
    if (!Array.isArray(entries)) {
      continue;
    }
    for (const entry of entries) {
      if (!Array.isArray(entry?.hooks)) {
        continue;
      }
      const kept = entry.hooks.filter((hook) => !isRetiredHookCommand(hook?.command));
      if (kept.length !== entry.hooks.length) {
        entry.hooks = kept;
        changed = true;
      }
    }
    const nonEmpty = entries.filter((entry) => !Array.isArray(entry?.hooks) || entry.hooks.length > 0);
    if (nonEmpty.length !== entries.length) {
      settings.hooks[event] = nonEmpty;
      changed = true;
    }
  }
  return changed;
}

function isRetiredHookCommand(command) {
  if (typeof command !== "string" || !command) {
    return false;
  }
  const normalized = unquoteShellWord(command).replaceAll("\\", "/").replaceAll("'", "").replaceAll('"', "");
  return RETIRED_MANAGED_HOOK_SCRIPTS.some(
    (name) => normalized.endsWith(`/scripts/hooks/${name}`) || normalized === `scripts/hooks/${name}`,
  );
}

function managedHookScriptName(command) {
  const normalized = unquoteShellWord(command).replaceAll("\\", "/").replaceAll("'", "").replaceAll('"', "");
  for (const scriptName of ["guard-protected-branch.sh", "show-phase-status.sh", "comment-checker.py", "record-skill-read.py"]) {
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
  for (const scriptName of ["guard-protected-branch.sh", "show-phase-status.sh", "comment-checker.py", "record-skill-read.py"]) {
    if (normalized === `${normalizedRoot}/.agent-flow/scripts/hooks/${scriptName}`) {
      return scriptName;
    }
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
            { type: "command", command: hookScriptCommand(root, "guard-protected-branch.sh") },
          ],
        },
      ],
      PostToolUse: [
        {
          matcher: "^(apply_patch|Write|Edit|MultiEdit|write|edit|multi_edit|multiedit)$",
          hooks: [{ type: "command", command: hookScriptCommand(root, "comment-checker.py") }],
        },
        {
          matcher: READ_TOOL_MATCHER,
          hooks: [{ type: "command", command: hookScriptCommand(root, "record-skill-read.py") }],
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
  pruneRetiredHooks(settings);
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
    fs.mkdirSync(path.dirname(settingsPath), { recursive: true });
    fs.writeFileSync(settingsPath, `${JSON.stringify(settings, null, 2)}\n`, "utf8");
  }
  installCodexTrustState(root);
}

function claudeHooksSettings(root) {
  return {
    hooks: {
      PreToolUse: [
        {
          matcher: "Bash",
          hooks: [
            { type: "command", command: hookScriptCommand(root, "guard-protected-branch.sh") },
          ],
        },
      ],
      PostToolUse: [
        {
          matcher: "^(apply_patch|Write|Edit|MultiEdit|write|edit|multi_edit|multiedit)$",
          hooks: [{ type: "command", command: hookScriptCommand(root, "comment-checker.py") }],
        },
        {
          matcher: READ_TOOL_MATCHER,
          hooks: [{ type: "command", command: hookScriptCommand(root, "record-skill-read.py") }],
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
  fs.mkdirSync(path.dirname(settingsPath), { recursive: true });
  fs.writeFileSync(settingsPath, `${JSON.stringify(settings, null, 2)}\n`, "utf8");
}

function ompHooksExtensionSource() {
  return String.raw`// ${OMP_EXTENSION_MARKER}
import fs from "node:fs";
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const HOOK_DIR = path.join(ROOT, ".agent-flow", "scripts", "hooks");
const WRITE_TOOL_RE = /^(apply_patch|Write|Edit|MultiEdit|write|edit|multi_edit|multiedit)$/i;
const READ_TOOL_RE = /^(Read|read|read_file|view|cat)$/i;

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
    for (const scriptName of ["guard-protected-branch.sh"]) {
      const result = await runHook(scriptName, payload, ctx);
      if (result.block) {
        return { block: true, reason: result.reason };
      }
    }
  });

  // tool_result 핸들러는 **하나만** 둔다. 이벤트당 하나만 유지하는 host가 있어
  // 두 번 등록하면 뒤엣것이 앞엣것을 조용히 덮는다.
  pi.on("tool_result", async (event, ctx) => {
    const toolName = String(event?.toolName || "");
    if (READ_TOOL_RE.test(toolName)) {
      // 관측 전용이다. 결과를 보지 않고, 어떤 경우에도 read를 막지 않는다.
      await runHook("record-skill-read.py", hookPayload(event, ctx), ctx);
      return;
    }
    if (!WRITE_TOOL_RE.test(toolName)) {
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

function upgradeBundledProfiles(root, src, dest) {
  if (!fs.existsSync(src)) {
    return;
  }
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    if (!entry.isFile()) {
      continue;
    }
    const content = fs.readFileSync(path.join(src, entry.name), "utf8");
    const target = path.join(dest, entry.name);
    backupIfDifferent(root, target, content);
    fs.writeFileSync(target, content, "utf8");
  }
}

function nextFreeBackupPath(base, content) {
  // 이미 같은 내용이 백업돼 있으면 사본을 늘리지 않는다. 다르면 비어 있는
  // 다음 이름을 찾는다. 무엇도 덮지 않으므로 어떤 버전도 잃지 않는다.
  for (let i = 0; i < 100; i += 1) {
    const candidate = i === 0 ? base : `${base}.${i}`;
    if (!fs.existsSync(candidate)) {
      return candidate;
    }
    if (fs.readFileSync(candidate, "utf8") === content) {
      return null;
    }
  }
  return null;
}

function backupIfDifferent(root, target, content) {
  if (!fs.existsSync(target)) {
    return;
  }
  if (fs.readFileSync(target, "utf8") === content) {
    return;
  }
  // 덮어쓰는 내용을 잃지 않는다. 고정 이름 하나만 쓰면 둘 중 하나를 반드시
  // 버리게 된다 — 매번 덮으면 사용자 원본이, 안 덮으면 이번 편집이 사라진다.
  const backup = nextFreeBackupPath(`${target}.bak`, fs.readFileSync(target, "utf8"));
  if (backup === null) {
    return;  // 같은 내용이 이미 백업돼 있다.
  }
  fs.copyFileSync(target, backup);
  console.log(`  ~ replaced ${path.relative(root, target)} (backup: ${path.relative(root, backup)})`);
}

function ompExtensionIsKitOwned(target) {
  if (!fs.existsSync(target)) {
    return true;
  }
  const current = fs.readFileSync(target, "utf8");
  // 표식은 이번 버전부터 붙는다. 그 이전 설치본에는 없으므로 생성 서명으로도
  // 인정한다. 이게 없으면 기존 사용자는 첫 업그레이드에서 영영 막힌다.
  return (
    current.includes(OMP_EXTENSION_MARKER) ||
    current.includes("export default function agentFlowHooks(")
  );
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

function pruneRetiredHookScripts(root) {
  // 설정에서만 빼면 실행 파일이 디스크에 남는다. 남은 파일을 다른 경로가
  // 다시 집어 실행하면 은퇴시킨 guard가 되살아난다.
  const hooksDir = path.join(root, ".agent-flow", "scripts", "hooks");
  for (const scriptName of RETIRED_MANAGED_HOOK_SCRIPTS) {
    const target = path.join(hooksDir, scriptName);
    if (fs.existsSync(target)) {
      // 사용자가 같은 이름으로 자기 스크립트를 뒀을 수 있다. 되돌릴 수 있게
      // 사본을 남기고 지운다. 설치본이 관리하지 않는 host 설정이 이 경로를
      // 여전히 가리킬 수 있으므로 경로를 함께 알린다.
      const kept = nextFreeBackupPath(`${target}.removed`, fs.readFileSync(target, "utf8"));
      if (kept !== null) {
        fs.copyFileSync(target, kept);
        // 실행 권한은 떼어 둔다. 되살릴 수 있게 남기는 사본이지 실행 대상이 아니다.
        fs.chmodSync(kept, 0o644);
      }
      fs.rmSync(target, { force: true });
      console.log(
        `  - removed retired hook: ${path.relative(root, target)} ` +
          `(backup: ${path.relative(root, target)}.removed)`,
      );
    }
  }
}

function makeHooksExecutable(root) {
  const hooksDir = path.join(root, ".agent-flow", "scripts", "hooks");
  if (!fs.existsSync(hooksDir)) {
    return;
  }
  for (const entry of fs.readdirSync(hooksDir)) {
    if (entry.endsWith(".sh") || entry.endsWith(".py")) {
      fs.chmodSync(path.join(hooksDir, entry), 0o755);
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
- Required review happens before completion QA. After reviewer approve, gates run BUILD -> TYPECHECK -> LINT where applicable. If review or QA fails, fix-loop routes back through comment-authoring and review before gates run again.
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
    root ? installedPythonRuntimePath(root) : "",
    path.join(KIT_ROOT, "src"),
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

  console.error("usage: agent-flow-kit install [--force-managed] | gates [--profile <id>] [--worktree <name>] | architecture-lint [--profile <id>] [--files ...] | run <install|start|status|next|advance|push-watch|push-watch-tick>");
  process.exit(1);
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
}
