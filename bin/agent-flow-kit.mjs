#!/usr/bin/env node

import fs from "node:fs";
import crypto from "node:crypto";
import path from "node:path";
import process from "node:process";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const command = process.argv[2];
const AGENT_FLOW_COMMAND = "agent-flow";
const HOME = process.env.HOME || process.env.USERPROFILE || "";
const KIT_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const installArgs = process.argv.slice(3);
const forceManaged = installArgs.includes("--force-managed");
let cachedFullFeatureWorkflow = null;
const BUNDLED_HOST_SKILL_NAMES = new Set([
  "agent-flow",
  "android-appshell-error-handling",
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
  const existingPayload = readExistingKit(agentFlowDir);
  const previousSkillIndex = readJsonIfExists(path.join(agentFlowDir, "skills", "index.json"));
  const phases = fullFeaturePhases();

  for (const name of ["runs", "state", "handoffs", "team", "worktrees", "workflows", "skills", "prompts", "rules", "bootstrap"]) {
    fs.mkdirSync(path.join(agentFlowDir, name), { recursive: true });
  }
  fs.mkdirSync(path.join(agentFlowDir, "local-skills"), { recursive: true });

  fs.mkdirSync(path.join(agentFlowDir, "skills", "agent-flow"), { recursive: true });
  fs.mkdirSync(path.join(agentFlowDir, "skills", "full-feature-workflow"), { recursive: true });

  const payload = {
    install_scope: "project",
    profile,
    root: ".",
    installed_at: existingPayload?.installed_at || new Date().toISOString(),
  };

  writeManagedFile(path.join(agentFlowDir, "workflows", "full-feature.yaml"), fullFeatureWorkflowYaml());
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, "workflows"), path.join(agentFlowDir, "workflows"), forceManaged);
  const agentFlowSkill = agentFlowSkillMarkdown();
  writeManagedFile(path.join(agentFlowDir, "skills", "agent-flow", "SKILL.md"), agentFlowSkill);
  writeManagedFile(
    path.join(agentFlowDir, "skills", "full-feature-workflow", "SKILL.md"),
    fullFeatureSkillMarkdown(),
  );
  writeManagedFile(path.join(agentFlowDir, "skills", "domain-grill", "SKILL.md"), domainGrillSkillMarkdown());
  for (const name of ["CONTEXT-FORMAT.md", "ADR-FORMAT.md"]) {
    writeManagedFile(
      path.join(agentFlowDir, "skills", "domain-grill", name),
      fs.readFileSync(path.join(KIT_ROOT, "skills", "grill-with-docs", name), "utf8"),
    );
  }
  writeManagedFile(path.join(agentFlowDir, "skills", "product-brief", "SKILL.md"), productBriefSkillMarkdown());
  writeManagedFile(path.join(agentFlowDir, "skills", "plan-reviewer", "SKILL.md"), planReviewerSkillMarkdown());
  writeManagedFile(
    path.join(agentFlowDir, "skills", "ddd-clean-architecture", "SKILL.md"),
    dddCleanArchitectureSkillMarkdown(),
  );
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
  );
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, "profiles"), path.join(agentFlowDir, "profiles"), forceManaged);
  const skillIndex = installProjectSkills(root, agentFlowDir, previousSkillIndex);
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, "scripts"), path.join(root, "scripts"), forceManaged);
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, ".Codex", "agents"), path.join(root, ".Codex", "agents"), forceManaged);
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, ".Codex", "rules", "context"), path.join(root, ".Codex", "rules", "context"), forceManaged);
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, ".Codex", "context"), path.join(root, ".Codex", "context"), forceManaged);
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
  writeManagedFile(path.join(agentFlowDir, "bootstrap", "GEMINI.md"), bootstrapMarkdown("GEMINI.md"));
  const gitignorePath = path.join(root, ".gitignore");
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
  removeLegacyProjectSkillCopies(root, "graphify");
  upsertBootstrapBlock(path.join(root, "AGENTS.md"), "AGENTS.md");
  upsertBootstrapBlock(path.join(root, "CLAUDE.md"), "CLAUDE.md");
  upsertBootstrapBlock(path.join(root, "GEMINI.md"), "GEMINI.md");
  makeHooksExecutable(root);
  installClaudeHooks(root);

  payload.skill_index = {
    path: ".agent-flow/skills/index.json",
    skills: skillIndex.skills.length,
    conflicts: skillIndex.conflicts.length,
    warnings: skillIndex.warnings.length,
  };

  fs.writeFileSync(path.join(agentFlowDir, "kit.json"), `${JSON.stringify(payload, null, 2)}\n`, "utf8");
  console.log(`agent-flow installed profile=${profile}`);
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
    if (!["default", "full-feature"].includes(workflow)) {
      throw new Error(`unknown workflow: ${workflow}`);
    }
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
    printNext(state);
    return;
  }

  if (subcommand === "status") {
    const state = readCurrentRun(root);
    printStatus(state, root);
    return;
  }

  if (subcommand === "next") {
    printNext(readCurrentRun(root));
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
    assertCompletionMarkers(phase, artifact);
    const nextIndex = nextPhaseIndex(state, phases, phase, artifact);
    const nextPhase = phases[nextIndex];
    const transitionedAt = new Date().toISOString();
    const fixLoopRounds = phase.id === "fix-loop"
      ? (state.fix_loop_rounds ?? 0) + 1
      : nextPhase?.id === "gates" && phase.id !== "fix-loop"
        ? 0
        : state.fix_loop_rounds;
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
      printNext(nextState);
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
  const packagePath = path.join(rootDir, "package.json");
  if (fs.existsSync(packagePath)) {
    const packageText = fs.readFileSync(packagePath, "utf8");
    if (packageText.includes("react-native")) {
      return "react-native";
    }
    if (packageText.includes("next")) {
      return "nextjs";
    }
    // 일반 TypeScript 프로젝트는 node보다 좁은 profile을 써야 gate와 skill routing이 맞다.
    if (fs.existsSync(path.join(rootDir, "tsconfig.json"))) {
      return "typescript";
    }
    return "node";
  }
  if (fs.existsSync(path.join(rootDir, "pyproject.toml"))) {
    return "python";
  }
  if (
    fs.existsSync(path.join(rootDir, "build.gradle")) ||
    fs.existsSync(path.join(rootDir, "settings.gradle")) ||
    fs.existsSync(path.join(rootDir, "build.gradle.kts")) ||
    fs.existsSync(path.join(rootDir, "settings.gradle.kts"))
  ) {
    return "android";
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
  const markers = new Set([".agent-flow", ".codex", ".Codex"]);
  for (let index = parts.length - 2; index >= 0; index -= 1) {
    if (parts[index + 1] !== "worktrees") continue;
    if (!markers.has(parts[index])) continue;
    const root = parts.slice(0, index).join(path.sep) || path.sep;
    // 홈의 전역 Codex worktree는 프로젝트 내부 worktree가 아니다.
    if (HOME && samePath(root, HOME) && (parts[index] === ".codex" || parts[index] === ".Codex")) {
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
  const required = [
    path.join(root, ".agent-flow", "kit.json"),
    path.join(root, ".agent-flow", "workflows", "full-feature.yaml"),
    path.join(root, ".agent-flow", "skills", "full-feature-workflow", "SKILL.md"),
    ...phases.map((phase) => path.join(root, ".agent-flow", "prompts", `${phase.id}.md`)),
    path.join(root, ".agent-flow", "skills", "domain-grill", "SKILL.md"),
    path.join(root, ".agent-flow", "skills", "product-brief", "SKILL.md"),
    path.join(root, ".agent-flow", "skills", "plan-reviewer", "SKILL.md"),
    path.join(root, ".agent-flow", "skills", "ddd-clean-architecture", "SKILL.md"),
    path.join(root, ".agent-flow", "skills", "clean-architecture", "SKILL.md"),
    path.join(root, ".agent-flow", "skills", "architecture-reviewer", "SKILL.md"),
    path.join(root, ".agent-flow", "skills", "push-watch", "SKILL.md"),
    path.join(root, ".agent-flow", "skills", "code-generation-discipline", "SKILL.md"),
    path.join(root, ".agent-flow", "skills", "react-development-guide", "SKILL.md"),
    path.join(root, ".agent-flow", "skills", "react-native-development-guide", "SKILL.md"),
    path.join(root, ".agent-flow", "prompts", "push-watch.md"),
    path.join(root, ".agent-flow", "prompts", "push-watch-tick.md"),
    path.join(root, ".agent-flow", "bootstrap", "AGENTS.md"),
    path.join(root, ".agent-flow", "bootstrap", "CLAUDE.md"),
    path.join(root, ".agent-flow", "bootstrap", "GEMINI.md"),
    path.join(root, ".Codex", "agents", "code-reviewer.md"),
  ];
  const missing = required.filter((pathName) => !fs.existsSync(pathName));
  if (missing.length > 0) {
    throw new Error(`agent-flow is not installed. run: agent-flow-kit install`);
  }
  const codeReviewer = path.join(root, ".Codex", "agents", "code-reviewer.md");
  if (!fs.readFileSync(codeReviewer, "utf8").trim()) {
    throw new Error("agent-flow is not installed correctly: .Codex/agents/code-reviewer.md is empty");
  }
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

function printNext(state) {
  const phase = workflowPhases(state.workflow)[state.phase_index];
  if (!phase) {
    console.log(`workflow complete: ${state.run_id}`);
    return;
  }
  console.log(`Current phase: ${phase.id}`);
  console.log(`Run: ${state.run_id}`);
  console.log(`Required artifact: ${path.join(state.run_dir, phase.artifact)}`);
  console.log(`Instruction: ${phase.instruction}`);
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
    const missing = missingMarkers(
      fs.readFileSync(resolvedRequiredArtifact, "utf8"),
      phase.required_markers ?? [],
    );
    status = "blocked";
    if (missing.length > 0) {
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

function copyBundledDirIfMissingOrSame(src, dest, force = false, excludedRootDirs = new Set(), isRoot = true) {
  if (!fs.existsSync(src)) {
    return;
  }
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const srcPath = path.join(src, entry.name);
    const destPath = path.join(dest, entry.name);
    if (entry.isDirectory()) {
      if (isRoot && excludedRootDirs.has(entry.name)) {
        removeManagedDirIfSame(srcPath, destPath, force);
        continue;
      }
      copyBundledDirIfMissingOrSame(srcPath, destPath, force, excludedRootDirs, false);
      continue;
    }
    if (!entry.isFile()) {
      continue;
    }
    const content = fs.readFileSync(srcPath, "utf8");
    writeManagedFileIfMissingOrSame(destPath, content, force);
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

function installProjectSkills(root, agentFlowDir, previousIndex) {
  const selected = selectProjectSkills(root, agentFlowDir);
  const links = [];
  for (const skill of selected.skills) {
    for (const host of skill.hosts) {
      links.push(linkProjectSkill(root, skill, host, previousIndex));
    }
  }
  links.push(...removeStaleProjectSkillLinks(root, selected.skills, previousIndex));
  const index = { ...selected, links };
  fs.writeFileSync(
    path.join(agentFlowDir, "skills", "index.json"),
    `${JSON.stringify(index, null, 2)}\n`,
    "utf8",
  );
  return index;
}

function selectProjectSkills(root, agentFlowDir) {
  const discovered = [
    ...discoverSkills(path.join(agentFlowDir, "local-skills"), "local", root, PROFILE_MANAGED_HOST_ONLY_SKILLS),
    ...discoverProjectSkills(root),
    ...discoverSkills(
      path.join(agentFlowDir, "skills"),
      "bundled",
      root,
      new Set(["index.json"]),
      BUNDLED_HOST_SKILL_NAMES,
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
  const skills = [...byName.values()].sort((a, b) => a.name.localeCompare(b.name));
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
  if (name !== parsedName) {
    warnings.push(`unsafe skill name ignored: ${parsedName}`);
  }
  const hostValues = Array.isArray(metadata.hosts) ? metadata.hosts : [];
  const knownHosts = new Set(["claude", "codex", "gemini", "antigravity"]);
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
    name,
    description: String(metadata.description || useWhen || ""),
    hosts: hostValues.length > 0 ? [...new Set(hosts)] : ["claude", "codex", "gemini", "antigravity"],
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

function removeStaleProjectSkillLinks(root, skills, previousIndex) {
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
    const hostRoot = hostSkillRoot(root, link.host);
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

function linkProjectSkill(root, skill, host, previousIndex) {
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
      if (currentHash !== skill.hash && currentHash !== previousHash) {
        return { name: skill.name, host, path: path.relative(root, destDir), status: "skipped-user-modified" };
      }
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
  if (host === "antigravity") {
    return path.join(root, ".gemini", "antigravity", "skills");
  }
  return path.join(root, `.${host}`, "skills");
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
  const candidates = [
    process.env.PYTHON,
    process.env.PYTHON_EXECUTABLE,
    virtualEnvPython,
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
  return "python3";
}

function pythonSupportsWorkflowExport(candidate) {
  const result = safeSpawnSync(candidate, ["-c", "import yaml"], {
    stdio: "ignore",
    timeout: 5_000,
  });
  return !result.error && result.status === 0;
}

function assertFreshArtifact(state, phase, artifact) {
  if (!FRESH_ARTIFACT_PHASE_IDS.has(phase.id)) {
    return;
  }
  const enteredAt = Date.parse(state.phase_entered_at ?? state.updated_at ?? state.started_at ?? "");
  if (!Number.isFinite(enteredAt)) {
    return;
  }
  const artifactMtime = fs.statSync(artifact).mtimeMs;
  if (artifactMtime < enteredAt) {
    throw new Error(`blocked: stale artifact ${artifact}`);
  }
}

function assertCompletionMarkers(phase, artifact) {
  const markers = phase.required_markers ?? [];
  if (markers.length === 0) {
    return;
  }
  const content = fs.readFileSync(artifact, "utf8");
  const missing = missingMarkers(content, markers);
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
      out.push(lowered);
    }
  }
  return out;
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
    return lineKey === markerKey && allowed.includes(line.slice(lineSeparator + 1).trim());
  }
  return line === marker;
}

function artifactHasFailureMarkers(pathName) {
  const content = fs.readFileSync(pathName, "utf8");
  return completionGateLines(content).some((line) => {
    const separator = line.indexOf(":");
    return separator !== -1 && line.slice(separator + 1).trim() === "fail";
  });
}

const FIX_LOOP_MAX_ROUNDS = 3;

function nextPhaseIndex(state, phases, phase, artifact) {
  if (phase.id === "fix-loop") {
    const rounds = (state.fix_loop_rounds ?? 0) + 1;
    if (rounds > FIX_LOOP_MAX_ROUNDS) {
      throw new Error(`blocked: fix-loop exceeded ${FIX_LOOP_MAX_ROUNDS} rounds — escalate to user`);
    }
  }
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
  return phaseIndex(phases, target);
}

function nodeRouteKey(phase, artifact) {
  if (phase.id === "gates") {
    return readGatesPassed(artifact) ? "green" : "request-changes";
  }
  if (phase.multi_review) {
    const verdict = readMultiReviewVerdict(artifact);
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
    if (["green", "merged", "skipped", "comments", "ci-failed", "pending", "closed", "error"].includes(status)) {
      return status;
    }
    throw new Error("blocked: pr-watch artifact must include status: green, comments, ci-failed, skipped, merged, or pending");
  }
  if (phase.id === "plan-review" || phase.id === "architecture-review" || phase.id === "merge-approval") {
    const verdict = readArtifactVerdict(artifact);
    if (verdict) {
      if (verdict === "approve" && phase.routes?.["request-changes"] && artifactHasFailureMarkers(artifact)) {
        return "request-changes";
      }
      return verdict;
    }
    throw new Error(`blocked: ${phase.id} artifact must include verdict`);
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
  const status = match?.[1]?.toLowerCase();
  if (status === "has_comments" || status === "has-comments") return "comments";
  if (status === "ci_failed") return "ci-failed";
  return status;
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

function readMultiReviewVerdict(pathName) {
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
  if (overall === "approve" && reviewers.size < 2) {
    throw new Error("blocked: multi-review approve requires at least 2 independent sub-agent reviewer verdicts");
  }
  if (overall === "approve" && verdicts.every((verdict) => verdict === "approve")) {
    return "approve";
  }
  if (overall === "request-changes" || (overall && verdicts.includes("request-changes"))) {
    return "request-changes";
  }
  throw new Error("blocked: multi-review artifact must include matching reviewer verdicts and overall verdict");
}

function readMultiReviewOverallVerdict(content) {
  const sections = content.split(/^##[ \t]+Overall[ \t]*$/im);
  if (sections.length < 2) {
    return undefined;
  }
  if (sections.length > 2) {
    return "invalid-verdict";
  }
  const overallBlock = sections[sections.length - 1].split(/^#{1,6}[ \t]+/m, 1)[0] ?? "";
  const verdicts = [...overallBlock.matchAll(/^verdict:\s*([a-z-]+)\s*$/gim)]
    .map((match) => match[1].toLowerCase());
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
    stateFor(reviewerId).verdict = match[2].toLowerCase();
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
    const verdict = reviewerBlock.match(/^\s*verdict:\s*(approve|request-changes)\s*$/im)?.[1]?.toLowerCase();
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
  return normalized === "sub agent" || normalized === "subagent";
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
  // Reviewer heading은 numbered/lettered reviewer만 독립 id로 인정한다.
  const key = normalizeReviewerId(value);
  return /^(?:[0-9]+|[a-z])$/.test(key) ? key : "";
}

function readGatesPassed(pathName) {
  try {
    const content = fs.readFileSync(pathName, "utf8");
    const data = JSON.parse(content);
    if (typeof data.passed !== "boolean" || !Array.isArray(data.results) || data.results.length === 0) {
      return false;
    }
    // 완료 보고는 실제 실행한 gate command와 결과 evidence가 함께 있을 때만 허용한다.
    const resultsPass = data.results.every((r) =>
      r &&
      typeof r.command === "string" &&
      r.command.trim().length > 0 &&
      hasGateEvidence(r) &&
      (r.passed === true || r.status === "pass" || r.status === "ok"),
    );
    return data.passed && resultsPass;
  } catch {
    return false;
  }
}

function hasGateEvidence(result) {
  for (const key of ["output", "stdout", "stderr", "artifact", "path"]) {
    if (typeof result[key] === "string" && result[key].trim().length > 0) {
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
- \`default.yaml\`: design → slice-plan → worktree → implement → final-review ↔ fix-loop → commit → push-pr → pr-watch → merge → cleanup
- \`full-feature.yaml\`: domain-grill → product-brief → prd → slice-plan → plan-review → ddd-design → worktree → run-start → red → green → refactor → gates ↔ fix-loop → multi-review → architecture-review → commit → push-pr → pr-watch ↔ pr-comment-fix/pr-ci-fix → merge-approval → merge → handoff
- \`multi-review\`는 현재 사용 중인 CLI(활성 host)의 sub-agent 2개가 필수다. 두 sub-agent를 병렬 실행하고, \`reviewer-source: sub-agent\`를 기록한 뒤 sub-agent를 닫는다. 마지막에 \`## Overall\`과 \`verdict: approve\` 또는 \`verdict: request-changes\`만 기록한다. 활성 host가 아닌 추가 provider는 optional이다.

### Context Economy

- Codex / Claude / Gemini / Antigravity user-facing 답변은 기본적으로 짧은 한글로 한다.
- 코드/명령/식별자는 영어 그대로 유지한다.
- 긴 설명, 긴 로그, 전체 파일 붙여넣기 금지.
- 필요한 경우만 current phase, action, \`next_command\`, blocker를 요약한다.
- 모든 guide를 항상 로드하지 말고 변경 파일에 필요한 guide만 읽는다.
- 프로젝트 skill은 \`skills/<name>/SKILL.md\` 또는 private \`.agent-flow/local-skills/<name>/SKILL.md\`에 둔다.
- install/bootstrap 후 \`.agent-flow/skills/index.json\` metadata를 보고 필요한 skill만 읽는다. 모든 SKILL.md 전문을 항상 읽지 않는다.
- Claude/Codex/Gemini/Antigravity 프로젝트 skill 경로는 leader checkout의 install 결과를 따른다. worktree 안에서 install, index 재생성, skill link 재생성을 하지 않는다.
- Claude hook이 자동 차단하는 보호 브랜치 commit/push와 leader checkout/switch 금지는 Codex에서도 동일하게 지킨다.

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
- \`default.yaml\`: design → slice-plan → worktree → implement → final-review ↔ fix-loop → commit → push-pr → pr-watch → merge → cleanup
- \`full-feature.yaml\`: domain-grill → product-brief → prd → slice-plan → plan-review → ddd-design → worktree → run-start → red → green → refactor → gates ↔ fix-loop → multi-review → architecture-review → commit → push-pr → pr-watch ↔ pr-comment-fix/pr-ci-fix → merge-approval → merge → handoff
- \`multi-review\`는 현재 사용 중인 CLI(활성 host)의 sub-agent 2개가 필수다. 두 sub-agent를 병렬 실행하고, \`reviewer-source: sub-agent\`를 기록한 뒤 sub-agent를 닫는다. 마지막에 \`## Overall\`과 \`verdict: approve\` 또는 \`verdict: request-changes\`만 기록한다. 활성 host가 아닌 추가 provider는 optional이다.

### Context Economy

- Codex / Claude / Gemini / Antigravity user-facing 답변은 기본적으로 짧은 한글로 한다.
- 코드/명령/식별자는 영어 그대로 유지한다.
- 긴 설명, 긴 로그, 전체 파일 붙여넣기 금지.
- 필요한 경우만 current phase, action, \`next_command\`, blocker를 요약한다.
- 모든 guide를 항상 로드하지 말고 변경 파일에 필요한 guide만 읽는다.
- 프로젝트 skill은 \`skills/<name>/SKILL.md\` 또는 private \`.agent-flow/local-skills/<name>/SKILL.md\`에 둔다.
- install/bootstrap 후 \`.agent-flow/skills/index.json\` metadata를 보고 필요한 skill만 읽는다. 모든 SKILL.md 전문을 항상 읽지 않는다.
- Claude/Codex/Gemini/Antigravity 프로젝트 skill 경로는 leader checkout의 install 결과를 따른다. worktree 안에서 install, index 재생성, skill link 재생성을 하지 않는다.
- Claude hook이 자동 차단하는 보호 브랜치 commit/push와 leader checkout/switch 금지는 Codex에서도 동일하게 지킨다.

During code generation, modification, and code review phases, apply \`code-generation-discipline\`. Read every matching language/framework skill before writing or judging code.
For Android/Kotlin/Compose/KMP work, read every relevant local skill file from the Android profile's \`android_skills\` and \`chrisbanes_skills\` for the active host. Do not install, copy, link, or load duplicate skills from other host directories. If a required local skill is missing, report \`missing local <group>: <skill>\` with the profile source URL and ask the user to install it.
`;
}

function newRunId() {
  const stamp = new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
  return `${stamp}-${Math.random().toString(16).slice(2, 10)}`;
}

const FRESH_ARTIFACT_PHASE_IDS = new Set([
  "slice-plan",
  "plan-review",
  "refactor",
  "gates",
  "multi-review",
  "fix-loop",
  "architecture-review",
  "pr-watch",
  "pr-comment-fix",
  "pr-ci-fix",
]);

function fullFeatureWorkflowYaml() {
  return fullFeatureWorkflow().text;
}

function agentFlowSkillMarkdown() {
  return `---
name: agent-flow
description: Use when the user types /agent-flow, asks to start or continue the project workflow, or wants Claude, Codex, or Gemini to drive the agent-flow lifecycle.
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
- During code generation, modification, and code review phases, apply \`code-generation-discipline\`. Read every matching language/framework skill before writing or judging code. If a required local skill is missing, report it and wait for install or explicit override.
- Keep user-facing replies short Korean by default. Keep code, commands, paths, and identifiers in English.
- Do not paste long logs or whole files. Summarize only current phase, action, \`next_command\`, and blocker when useful.
`;
}

function fullFeatureSkillMarkdown() {
  return `---\nname: full-feature-workflow\ndescription: Use this skill for feature work in this project.\n---\n\n# Full Feature Workflow\n\nUse this skill for feature work in this project.\n\nAlways drive progress through the runner output. Run \`${AGENT_FLOW_COMMAND} status\`, then execute the printed \`next_command\` exactly.\n\nDo not skip phases. If existing docs satisfy a phase, write the required artifact and reference those docs. If a gate, review, PR comment, or PR check fails, complete the matching fix phase and push again before merge/handoff.\n\nApply \`code-generation-discipline\` during code and review phases. Read every matching language/framework skill before writing or judging code.\n`;
}

function domainGrillSkillMarkdown() {
  return fs.readFileSync(path.join(KIT_ROOT, "skills", "domain-grill", "SKILL.md"), "utf8");
}

function productBriefSkillMarkdown() {
  return `---\nname: product-brief\ndescription: Use during the full-feature product-brief phase.\n---\n\n# Product Brief\n\nUse during the full-feature product-brief phase.\n\nAsk YC-style forcing questions before implementation:\n\n1. Demand Reality: what behavior proves people want this?\n2. Status Quo: how do they solve it today?\n3. Desperate Specificity: who is the most painful target user?\n4. Narrowest Wedge: what is the smallest version worth using now?\n5. Observation: what concrete user behavior was observed?\n6. Future Fit: why is now the right time?\n\nArtifact template:\n\n# Product Brief\n\n## Mode\nstartup | builder | internal\n\n## Demand Evidence\n\n## Status Quo\n\n## Target User\n\n## Narrowest Wedge\n\n## Observed Behavior\n\n## Why Now\n\n## Cut List\n\n## Assignment\n\n## Decision\nbuild | defer | cut\n`;
}

function planReviewerSkillMarkdown() {
  return `---\nname: plan-reviewer\ndescription: Use during the full-feature plan-review phase.\n---\n\n# Plan Reviewer\n\nUse during the full-feature plan-review phase.\n\nReview only. Do not rewrite the plan.\n\nCheck:\n\n- Missing data collection steps.\n- Missing validation steps.\n- Wrong implementation order.\n- Oversized slices that should be split.\n- Missing state/storage steps.\n- Test coverage gaps.\n- Architecture risks before coding.\n\nArtifact template:\n\n# Plan Review\n\nverdict: approve | request-changes\n\n## Scope Checked\n\n## Missing Steps\n\n## Wrong Order\n\n## Oversized Slices\n\n## Validation Gaps\n\n## Data/State Gaps\n\n## Architecture Risks\n\n## Required Changes\n\n## Approval Notes\n`;
}

function dddCleanArchitectureSkillMarkdown() {
  return `---\nname: ddd-clean-architecture\ndescription: Legacy compatibility shim for full-feature ddd-design and architecture-review phases. Prefer ddd-architecture first, then clean-architecture.\n---\n\n# DDD Clean Architecture\n\nThis legacy skill delegates responsibility:\n\n1. Apply \`skills/ddd-architecture/SKILL.md\` for Bounded Context, Ubiquitous Language, Entities, Value Objects, Aggregates, Domain Events, Domain Invariants, and Domain Flow.\n2. Apply \`skills/clean-architecture/SKILL.md\` for layer boundaries, dependency direction, UseCase ports/impls, Repository ports/adapters, Cache, Mapper, Composition Root, Testability, and SOLID architecture validation.\n\nDo not treat DDD and Clean Architecture as one responsibility. DDD defines the domain; Clean Architecture protects it with boundaries.\n`;
}

function architectureReviewerSkillMarkdown() {
  return `---\nname: architecture-reviewer\ndescription: Use during the full-feature architecture-review phase.\n---\n\n# Architecture Reviewer\n\nUse during the full-feature architecture-review phase.\n\nReview implemented code against domain decisions and DDD/Clean Architecture.\n\nArtifact template:\n\n# Architecture Review\n\nverdict: approve | request-changes\n\n## Domain Alignment\n\n## Layer Violations\n\n## Repository Boundary Issues\n\n## Dependency Direction Issues\n\n## Required Refactors\n\n## Approved Exceptions\n`;
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
  return `# ${phase.id}\n\n${phase.instruction}${markers}\n\nSave the required artifact before running:\n\n\`\`\`bash\n${AGENT_FLOW_COMMAND} run advance\n\`\`\`\n`;
}

function shellQuote(value) {
  return `'${String(value).replaceAll("'", "'\\''")}'`;
}

function hookScriptCommand(root, scriptName) {
  return shellQuote(path.join(root, "scripts", "hooks", scriptName));
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
  const normalized = unquoteShellWord(command).replaceAll("\\", "/");
  for (const scriptName of ["guard-worktree.sh", "guard-protected-branch.sh", "show-phase-status.sh"]) {
    if (normalized === `scripts/hooks/${scriptName}` || normalized.endsWith(`/scripts/hooks/${scriptName}`)) {
      return scriptName;
    }
  }
  return null;
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
      Stop: [
        {
          matcher: "",
          hooks: [{ type: "command", command: hookScriptCommand(root, "show-phase-status.sh") }],
        },
      ],
    },
  };
}

function installClaudeHooks(root) {
  const settingsPath = path.join(root, ".claude", "settings.json");
  let settings = {};
  if (fs.existsSync(settingsPath)) {
    try {
      settings = JSON.parse(fs.readFileSync(settingsPath, "utf8"));
    } catch {
      settings = {};
    }
  }
  if (!settings.hooks) {
    settings.hooks = {};
  }
  const desired = claudeHooksSettings(root).hooks;
  for (const [event, entries] of Object.entries(desired)) {
    if (!settings.hooks[event]) {
      settings.hooks[event] = [];
    }
    for (const entry of entries) {
      const existing = settings.hooks[event].find((e) => e.matcher === entry.matcher);
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
  fs.mkdirSync(path.dirname(settingsPath), { recursive: true });
  fs.writeFileSync(settingsPath, `${JSON.stringify(settings, null, 2)}\n`, "utf8");
}

function makeHooksExecutable(root) {
  const hooksDir = path.join(root, "scripts", "hooks");
  if (!fs.existsSync(hooksDir)) {
    return;
  }
  for (const entry of fs.readdirSync(hooksDir)) {
    if (entry.endsWith(".sh")) {
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
- Apply \`code-generation-discipline\` during red, green, refactor, fix-loop, and review phases. Read every matching language/framework skill before writing or judging code.
- If review or QA fails, return to the fix phase before continuing.
- The gates->fix-loop->gates loop re-verifies after every fix. multi-review approve skips fix-loop; request-changes routes through fix-loop->gates.
- Code review requires at least two active-host sub-agents (Codex sub-agent in Codex, Claude sub-agent in Claude, Gemini sub-agent in Gemini). If the changed scope spans multiple areas, run one additional active-host sub-agent in parallel. Additional non-host providers are optional, and approve requires 2+ independent sub-agent reviewer verdicts with reviewer-source: sub-agent. After recording each sub-agent result, close that sub-agent session. End multi-review artifacts with ## Overall followed by exactly one verdict line: verdict: approve or verdict: request-changes.
- In the default workflow, gates are enforced by the \`implement\` phase completion marker: \`gates: all_passed\`.

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

  console.error("usage: agent-flow-kit install [--force-managed] | run <install|start|status|next|advance|push-watch|push-watch-tick>");
  process.exit(1);
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
}
