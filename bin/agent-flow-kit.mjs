#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { execFileSync, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const command = process.argv[2];
const AGENT_FLOW_COMMAND = "agent-flow";
const HOME = process.env.HOME || process.env.USERPROFILE || "";
const KIT_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const installArgs = process.argv.slice(3);

function installProject() {
  const root = resolveInstallRoot(process.cwd());
  const agentFlowDir = path.join(root, ".agent-flow");
  const profile = detectProfile(root);
  const existingPayload = readExistingKit(agentFlowDir);

  for (const name of ["runs", "state", "handoffs", "team", "worktrees", "workflows", "skills", "prompts", "rules", "bootstrap"]) {
    fs.mkdirSync(path.join(agentFlowDir, name), { recursive: true });
  }

  fs.mkdirSync(path.join(agentFlowDir, "skills", "agent-flow"), { recursive: true });
  fs.mkdirSync(path.join(agentFlowDir, "skills", "full-feature-workflow"), { recursive: true });

  const payload = {
    install_scope: "project",
    profile,
    root: ".",
    installed_at: existingPayload?.installed_at || new Date().toISOString(),
  };

  writeManagedFile(path.join(agentFlowDir, "workflows", "full-feature.yaml"), fullFeatureWorkflowYaml());
  const agentFlowSkill = agentFlowSkillMarkdown();
  writeManagedFile(path.join(agentFlowDir, "skills", "agent-flow", "SKILL.md"), agentFlowSkill);
  if (HOME) {
    writeManagedFileIfMissingOrSame(path.join(HOME, ".codex", "skills", "agent-flow", "SKILL.md"), agentFlowSkill);
    writeManagedFileIfMissingOrSame(path.join(HOME, ".claude", "skills", "agent-flow", "SKILL.md"), agentFlowSkill);
  }
  writeManagedFile(
    path.join(agentFlowDir, "skills", "full-feature-workflow", "SKILL.md"),
    fullFeatureSkillMarkdown(),
  );
  writeManagedFile(path.join(agentFlowDir, "skills", "domain-grill", "SKILL.md"), domainGrillSkillMarkdown());
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
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, "skills"), path.join(agentFlowDir, "skills"));
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, "scripts"), path.join(root, "scripts"));
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, ".Codex", "agents"), path.join(root, ".Codex", "agents"));
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, ".Codex", "rules", "context"), path.join(root, ".Codex", "rules", "context"));
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, ".Codex", "context"), path.join(root, ".Codex", "context"));
  writeManagedFileIfMissingOrSame(
    path.join(root, ".Codex", "rules", "codebase-rubric.md"),
    fs.readFileSync(path.join(KIT_ROOT, ".Codex", "rules", "codebase-rubric.md"), "utf8"),
  );
  writeManagedFile(path.join(agentFlowDir, "prompts", "push-watch.md"), pushWatchPromptMarkdown());
  writeManagedFile(path.join(agentFlowDir, "prompts", "push-watch-tick.md"), pushWatchTickPromptMarkdown());
  for (const phase of PHASES) {
    writeManagedFile(
      path.join(agentFlowDir, "prompts", `${phase.id}.md`),
      phasePrompt(phase),
    );
  }
  writeManagedFile(path.join(agentFlowDir, "rules", "workflow-contract.md"), workflowContract());
  writeManagedFile(path.join(agentFlowDir, "bootstrap", "AGENTS.md"), bootstrapMarkdown("AGENTS.md"));
  writeManagedFile(path.join(agentFlowDir, "bootstrap", "CLAUDE.md"), bootstrapMarkdown("CLAUDE.md"));
  writeManagedFile(path.join(agentFlowDir, "bootstrap", "GEMINI.md"), bootstrapMarkdown("GEMINI.md"));
  upsertGitignore(path.join(root, ".gitignore"), [
    ".agent-flow/",
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
    "graphify/",
    "agent-flow/",
    "graphify-out/manifest.json",
    "graphify-out/cost.json",
  ]);
  upsertBootstrapBlock(path.join(root, "AGENTS.md"), "AGENTS.md");
  upsertBootstrapBlock(path.join(root, "CLAUDE.md"), "CLAUDE.md");
  upsertBootstrapBlock(path.join(root, "GEMINI.md"), "GEMINI.md");
  makeHooksExecutable(root);
  installClaudeHooks(root);

  if (!installArgs.includes("--without-graphify")) {
    payload.graphify = installGraphify(root, existingPayload?.graphify);
  }

  fs.writeFileSync(path.join(agentFlowDir, "kit.json"), `${JSON.stringify(payload, null, 2)}\n`, "utf8");
  console.log(`agent-flow installed profile=${profile}`);
  if (payload.graphify) {
    console.log(`graphify installed status=${payload.graphify.status}`);
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
    if (workflow !== "full-feature") {
      throw new Error(`unknown workflow: ${workflow}`);
    }
    const runId = optionValue(args, "--run-id") ?? newRunId();
    assertInstalled(root);
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
      phase: PHASES[0].id,
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
    const phase = PHASES[state.phase_index];
    const artifact = path.join(runDir, phase.artifact);
    if (!fs.existsSync(artifact)) {
      throw new Error(`blocked: missing artifact ${artifact}`);
    }
    assertFreshArtifact(state, phase, artifact);
    assertCompletionMarkers(phase, artifact);
    const nextIndex = nextPhaseIndex(state, phase, artifact);
    const nextPhase = PHASES[nextIndex];
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
    return parts.slice(0, index).join(path.sep) || path.sep;
  }
  return null;
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
  const required = [
    path.join(root, ".agent-flow", "kit.json"),
    path.join(root, ".agent-flow", "workflows", "full-feature.yaml"),
    path.join(root, ".agent-flow", "skills", "full-feature-workflow", "SKILL.md"),
    ...PHASES.map((phase) => path.join(root, ".agent-flow", "prompts", `${phase.id}.md`)),
    path.join(root, ".agent-flow", "skills", "domain-grill", "SKILL.md"),
    path.join(root, ".agent-flow", "skills", "grill-with-docs", "SKILL.md"),
    path.join(root, ".agent-flow", "skills", "product-brief", "SKILL.md"),
    path.join(root, ".agent-flow", "skills", "plan-reviewer", "SKILL.md"),
    path.join(root, ".agent-flow", "skills", "ddd-clean-architecture", "SKILL.md"),
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
}

function normalizeRunState(root, state) {
  if (state.status === "complete" || state.phase === "complete") {
    return state;
  }
  const index = PHASES.findIndex((phase) => phase.id === state.phase);
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
  try {
    const branch = execFileSync("git", ["branch", "--show-current"], { cwd: root, encoding: "utf8" }).trim();
    if (!branch) {
      throw new Error("blocked: push-watch requires a named git branch");
    }
    return branch;
  } catch {
    throw new Error("blocked: push-watch requires a named git branch");
  }
}

function readPullRequestStatus(root) {
  const result = spawnSync("gh", ["pr", "view", "--json", "url,reviewDecision,statusCheckRollup"], {
    cwd: root,
    encoding: "utf8",
  });
  if (result.status !== 0) {
    throw new Error(`blocked: gh pr view failed${result.stderr ? `: ${result.stderr.trim()}` : ""}`);
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
  const phase = PHASES[state.phase_index];
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
  const phase = PHASES[state.phase_index];
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
        nextPhaseIndex(state, phase, resolvedRequiredArtifact);
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

function writeManagedFileIfMissingOrSame(pathName, content) {
  fs.mkdirSync(path.dirname(pathName), { recursive: true });
  if (fs.existsSync(pathName)) {
    const current = fs.readFileSync(pathName, "utf8");
    if (current !== content) {
      return false;
    }
    return true;
  }
  fs.writeFileSync(pathName, content, "utf8");
  return true;
}

function copyBundledDirIfMissingOrSame(src, dest) {
  if (!fs.existsSync(src)) {
    return;
  }
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const srcPath = path.join(src, entry.name);
    const destPath = path.join(dest, entry.name);
    if (entry.isDirectory()) {
      copyBundledDirIfMissingOrSame(srcPath, destPath);
      continue;
    }
    if (!entry.isFile()) {
      continue;
    }
    const content = fs.readFileSync(srcPath, "utf8");
    writeManagedFileIfMissingOrSame(destPath, content);
  }
}

function installGraphify(root, existingGraphify) {
  if (process.env.AGENT_FLOW_GRAPHIFY_DRY_RUN === "1") {
    return {
      status: "dry-run",
      package: "graphifyy",
      command: "graphify",
      platforms: ["claude", "codex", "gemini"],
      skill_location: "~/.agents/skills/graphify",
      removed_duplicate_skills: [],
      graph: {
        status: "dry-run",
        command: "graphify .",
        output: "graphify-out/",
      },
    };
  }
  if (existingGraphify && graphifyAvailable(root)) {
    return {
      ...existingGraphify,
      status: "reused",
    };
  }
  try {
    const installer = installGraphifyPackage(root);
    const skillInstall = runGraphifyInstall(root);
    const graph = runGraphifyProjectGraph(root);
    return {
      status: "installed",
      package: "graphifyy",
      command: "graphify",
      installer,
      platforms: skillInstall.platforms,
      skill_location: skillInstall.skillLocation,
      removed_duplicate_skills: skillInstall.removedDuplicates,
      graph,
    };
  } catch (error) {
    // graphify는 보조 인덱서라 실패해도 agent-flow 설치와 worktree 시작을 막지 않는다.
    return {
      status: "skipped",
      package: "graphifyy",
      command: "graphify",
      reason: formatGraphifyError(error),
      platforms: ["claude", "codex", "gemini"],
      skill_location: "~/.agents/skills/graphify",
      removed_duplicate_skills: [],
      graph: {
        status: "skipped",
        command: "graphify .",
        output: "graphify-out/",
      },
    };
  }
}

function formatGraphifyError(error) {
  return error instanceof Error ? error.message : String(error);
}

function runChecked(commandName, args, cwd) {
  const result = spawnSync(commandName, args, {
    cwd,
    stdio: "inherit",
    env: process.env,
  });
  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    throw new Error(`${commandName} ${args.join(" ")} failed with exit ${result.status}`);
  }
}

function runOptional(commandName, args, cwd) {
  const result = spawnSync(commandName, args, {
    cwd,
    stdio: "inherit",
    env: process.env,
  });
  if (result.error && result.error.code === "ENOENT") {
    return false;
  }
  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    throw new Error(`${commandName} ${args.join(" ")} failed with exit ${result.status}`);
  }
  return true;
}

function installGraphifyPackage(root) {
  if (graphifyAvailable(root)) {
    return "existing";
  }
  if (runCandidate("uv", ["tool", "install", "--python", "3.12", "--force", "graphifyy"], root)) {
    return "uv tool";
  }
  const python = preferredPython();
  if (runCandidate("pipx", ["install", "--python", python, "--force", "graphifyy"], root)) {
    return "pipx";
  }
  runChecked(python, ["-m", "pip", "install", "graphifyy"], root);
  return "pip";
}

function graphifyAvailable(root) {
  if (runCandidateQuiet("graphify", ["--help"], root) && runCandidateQuiet("graphify", ["install", "--help"], root)) {
    return true;
  }
  const graphify = graphifyExecutable();
  return (
    runCandidateQuiet(graphify.command, [...graphify.prefixArgs, "--help"], root) &&
    runCandidateQuiet(graphify.command, [...graphify.prefixArgs, "install", "--help"], root)
  );
}

function runGraphifyInstall(root) {
  runGraphifyCommand(["install"], root);
  runGraphifyCommand(["install", "--platform", "codex"], root);
  runGraphifyCommand(["install", "--platform", "gemini"], root);
  return {
    platforms: ["claude", "codex", "gemini"],
    skillLocation: "~/.agents/skills/graphify",
    removedDuplicates: canonicalizeGraphifySkill(),
  };
}

function canonicalizeGraphifySkill() {
  if (!HOME) {
    return [];
  }
  const canonical = path.join(HOME, ".agents", "skills", "graphify");
  const candidates = [
    canonical,
    path.join(HOME, ".codex", "skills", "graphify"),
    path.join(HOME, ".gemini", "skills", "graphify"),
    path.join(HOME, ".claude", "skills", "graphify"),
  ];
  const existing = candidates
    .filter((candidate) => fs.existsSync(path.join(candidate, "SKILL.md")))
    .sort((a, b) => {
      const aTime = fs.statSync(path.join(a, "SKILL.md")).mtimeMs;
      const bTime = fs.statSync(path.join(b, "SKILL.md")).mtimeMs;
      return bTime - aTime;
    });
  if (existing.length === 0) {
    throw new Error("graphify install completed, but no graphify skill was found");
  }
  const source = existing[0];
  if (source !== canonical) {
    fs.mkdirSync(path.dirname(canonical), { recursive: true });
    const tempCanonical = `${canonical}.tmp.${process.pid}`;
    fs.rmSync(tempCanonical, { recursive: true, force: true });
    fs.cpSync(source, tempCanonical, { recursive: true });
    fs.rmSync(canonical, { recursive: true, force: true });
    fs.renameSync(tempCanonical, canonical);
  }
  const removed = [];
  for (const duplicate of candidates.filter((candidate) => candidate !== canonical)) {
    if (fs.existsSync(duplicate)) {
      fs.rmSync(duplicate, { recursive: true, force: true });
      removed.push(duplicate.replace(`${HOME}/`, "~/"));
    }
  }
  return removed;
}

function runGraphifyProjectGraph(root) {
  runGraphifyCommand(["."], root);
  return {
    status: "generated",
    command: "graphify .",
    output: "graphify-out/",
  };
}

function runGraphifyCommand(args, root) {
  if (runOptional("graphify", args, root)) {
    return;
  }
  const graphify = graphifyExecutable();
  runChecked(graphify.command, [...graphify.prefixArgs, ...args], root);
}

function runCandidate(commandName, args, cwd) {
  const result = spawnSync(commandName, args, {
    cwd,
    stdio: "inherit",
    env: process.env,
  });
  if (result.error && result.error.code === "ENOENT") {
    return false;
  }
  if (result.error) {
    throw result.error;
  }
  return result.status === 0;
}

function runCandidateQuiet(commandName, args, cwd) {
  const result = spawnSync(commandName, args, {
    cwd,
    stdio: "ignore",
    env: process.env,
  });
  if (result.error) {
    return false;
  }
  return result.status === 0;
}

function preferredPython() {
  for (const candidate of ["python3.12", "python3.11", "python3.10", "python3", "python"]) {
    const result = spawnSync(candidate, ["--version"], { stdio: "ignore" });
    if (!result.error && result.status === 0) {
      return candidate;
    }
  }
  return "python3";
}

function graphifyExecutable() {
  for (const candidate of [
    path.join(process.env.UV_TOOL_BIN_DIR || "", "graphify"),
    path.join(process.env.PIPX_BIN_DIR || "", "graphify"),
    path.join(HOME, ".local", "bin", "graphify"),
  ]) {
    if (!candidate) {
      continue;
    }
    if (fs.existsSync(candidate)) {
      return { command: candidate, prefixArgs: [] };
    }
  }
  return { command: preferredPython(), prefixArgs: ["-m", "graphify"] };
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
    return !lines.some((line) => lineMatchesMarker(line, normalized));
  });
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

const FIX_LOOP_MAX_ROUNDS = 3;

const ROUTED_PHASE_IDS = new Set([
  "plan-review", "gates", "multi-review", "fix-loop",
  "architecture-review", "pr-watch", "merge-approval",
  "pr-comment-fix", "pr-ci-fix",
]);

function nextPhaseIndex(state, phase, artifact) {
  if (phase.id === "plan-review") {
    const verdict = readArtifactVerdict(artifact);
    if (verdict === "approve") {
      return state.phase_index + 1;
    }
    if (verdict === "request-changes") {
      return phaseIndex("slice-plan");
    }
    throw new Error("blocked: plan-review artifact must include verdict: approve or verdict: request-changes");
  }
  if (phase.id === "gates") {
    const passed = readGatesPassed(artifact);
    if (!passed) {
      return phaseIndex("fix-loop");
    }
    return state.phase_index + 1;
  }
  if (phase.id === "multi-review") {
    const verdict = readMultiReviewVerdict(artifact);
    if (verdict === "approve") {
      return phaseIndex("architecture-review");
    }
    if (verdict === "request-changes") {
      return phaseIndex("fix-loop");
    }
    throw new Error("blocked: multi-review artifact must include verdict: approve or verdict: request-changes");
  }
  if (phase.id === "fix-loop") {
    const rounds = (state.fix_loop_rounds ?? 0) + 1;
    if (rounds > FIX_LOOP_MAX_ROUNDS) {
      throw new Error(`blocked: fix-loop exceeded ${FIX_LOOP_MAX_ROUNDS} rounds — escalate to user`);
    }
    return phaseIndex("gates");
  }
  if (phase.id === "architecture-review") {
    const verdict = readArtifactVerdict(artifact);
    if (verdict === "approve") {
      return state.phase_index + 1;
    }
    if (verdict === "request-changes" || verdict === "blocked") {
      return phaseIndex("refactor");
    }
    throw new Error("blocked: architecture-review artifact must include verdict: approve, request-changes, or blocked");
  }
  if (phase.id === "pr-watch") {
    const status = readArtifactStatus(artifact);
    if (status === "green") {
      return phaseIndex("merge-approval");
    }
    if (status === "merged") {
      return phaseIndex("handoff");
    }
    if (status === "skipped") {
      return phaseIndex("handoff");
    }
    if (status === "comments") {
      return phaseIndex("pr-comment-fix");
    }
    if (status === "ci-failed") {
      return phaseIndex("pr-ci-fix");
    }
    if (status === "pending") {
      throw new Error("blocked: PR watch is pending");
    }
    throw new Error("blocked: pr-watch artifact must include status: green, comments, ci-failed, skipped, merged, or pending");
  }
  if (phase.id === "merge-approval") {
    const verdict = readArtifactVerdict(artifact);
    if (verdict === "approve") {
      return phaseIndex("merge");
    }
    throw new Error("blocked: merge-approval artifact must include verdict: approve");
  }
  if (phase.id === "pr-comment-fix" || phase.id === "pr-ci-fix") {
    return phaseIndex("pr-watch");
  }
  if (!ROUTED_PHASE_IDS.has(phase.id)) {
    return state.phase_index + 1;
  }
  throw new Error(`blocked: unhandled routing for phase ${phase.id}`);
}

function phaseIndex(id) {
  const index = PHASES.findIndex((phase) => phase.id === id);
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
  const reviewers = parseReviewerVerdicts(content);
  if (reviewers.size < 2) {
    throw new Error("blocked: multi-review artifact must contain at least 2 independent reviewer verdicts");
  }
  const overall = readArtifactVerdict(pathName);
  const verdicts = [...reviewers.values()];
  if (verdicts.includes("request-changes")) {
    return "request-changes";
  }
  if (overall === "approve" && verdicts.every((verdict) => verdict === "approve")) {
    return "approve";
  }
  if (overall === "request-changes") {
    return "request-changes";
  }
  throw new Error("blocked: multi-review artifact must include matching reviewer verdicts and overall verdict");
}

function parseReviewerVerdicts(content) {
  // reviewer id를 키로 정규화해 한 reviewer가 여러 번 approve를 찍어도 독립 리뷰로 세지 않는다.
  const reviewers = new Map();
  const linePattern = /^reviewer[-_ ]?([a-z0-9-]*)[^\n]*verdict:\s*(approve|request-changes)\s*$/gim;
  for (const match of content.matchAll(linePattern)) {
    const reviewerId = (match[1] || `line-${reviewers.size + 1}`).toLowerCase();
    reviewers.set(reviewerId, match[2].toLowerCase());
  }
  const sections = content.split(/^##\s+Reviewer\s*([^\n]*)/im);
  for (let index = 1; index < sections.length; index += 2) {
    const reviewerId = (sections[index].trim() || `section-${index}`).toLowerCase();
    const verdict = sections[index + 1]?.match(/verdict:\s*(approve|request-changes)/i)?.[1]?.toLowerCase();
    if (verdict) {
      reviewers.set(reviewerId, verdict);
    }
  }
  return reviewers;
}

function readGatesPassed(pathName) {
  try {
    const content = fs.readFileSync(pathName, "utf8");
    const data = JSON.parse(content);
    if (typeof data.passed !== "boolean" || !Array.isArray(data.results) || data.results.length === 0) {
      return false;
    }
    // 완료 보고는 실제 실행한 gate command와 결과가 함께 있을 때만 허용한다.
    const resultsPass = data.results.every((r) =>
      r &&
      typeof r.command === "string" &&
      r.command.trim().length > 0 &&
      (r.passed === true || r.status === "pass" || r.status === "ok"),
    );
    return data.passed && resultsPass;
  } catch {
    return false;
  }
}

function upsertBootstrapBlock(pathName, label) {
  const start = "<!-- agent-flow:start -->";
  const end = "<!-- agent-flow:end -->";
  const block = `${start}
## Agent Flow

Before feature work, run:

\`\`\`bash
${AGENT_FLOW_COMMAND} run "<task>"
\`\`\`

install은 프로젝트당 1회만 수행합니다. 새 세션이 시작됐다는 이유로 install을 다시 실행하지 않습니다.
Follow the CLI output exactly. Git projects start inside \`.agent-flow/worktrees/feat-<slug>/\` without switching the leader branch; continue with the printed \`next_command\`.

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

During code generation and modification phases, apply \`code-generation-discipline\`. Language-specific guide and comment rules follow the \`code-generation-discipline\` Before Starting checklist.
`;
}

function newRunId() {
  const stamp = new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
  return `${stamp}-${Math.random().toString(16).slice(2, 10)}`;
}

const PHASES = [
  {
    id: "domain-grill",
    artifact: "artifacts/domain-grill.md",
    required_markers: [
      "grill-with-docs: complete",
      "shared_understanding: reached",
      "context_docs_checked: true",
      "context_docs_updated: true|not_needed",
    ],
    instruction:
      "Run the internal grill-with-docs skill. Ask one question at a time unless code or docs answer it, challenge CONTEXT.md/ADR terminology, stress-test terms with concrete scenarios, cross-check user claims against code, update docs inline when terms resolve, normalize the compact domain map, and end with marker lines: grill-with-docs: complete, shared_understanding: reached, context_docs_checked: true, and context_docs_updated: true or not_needed.",
  },
  {
    id: "product-brief",
    artifact: "artifacts/product-brief.md",
    instruction:
      "Validate demand, status quo, target user, narrowest wedge, observed behavior, why now, cut list, and build/defer/cut decision.",
  },
  { id: "prd", artifact: "artifacts/prd.md", instruction: "Write the PRD before planning slices." },
  { id: "slice-plan", artifact: "artifacts/slice-plan.md", instruction: "Break the PRD into independently shippable slices." },
  {
    id: "plan-review",
    artifact: "artifacts/plan-review.md",
    instruction:
      "Review the slice plan like a senior reviewer. Record verdict: approve or verdict: request-changes with missing steps, wrong order, oversized slices, validation gaps, and required changes.",
  },
  {
    id: "ddd-design",
    artifact: "artifacts/ddd-design.md",
    instruction:
      "Design data / domain / presentation boundaries, domain core modules, repository interfaces, repository implementations, and dependency rules.",
  },
  {
    id: "worktree",
    artifact: "artifacts/worktree.md",
    instruction:
      "Run git worktree add -b feat/<slug> .agent-flow/worktrees/feat-<slug>/ main to create an isolated worktree without switching the leader branch. Do not just create a branch — a physical worktree is required for parallel task isolation. Do not work on main/master/develop. Follow profile.branching.naming when present. Record worktree path, branch name, and base commit.",
  },
  { id: "run-start", artifact: "artifacts/run-start.md", instruction: "Record the workflow run setup and selected provider." },
  {
    id: "red",
    artifact: "artifacts/red.log",
    instruction:
      "Apply code-generation-discipline before writing tests. Write failing tests first. Select python-development-guide, typescript-development-guide, react-development-guide, react-native-development-guide, or chrisbanes skills only when the changed files require them. Save the failure output.",
  },
  {
    id: "green",
    artifact: "artifacts/green.log",
    instruction:
      "Apply code-generation-discipline. Implement the minimum change, apply selected language-specific guides as secondary checklists, add required Korean code comments, and save passing test output.",
  },
  {
    id: "refactor",
    artifact: "artifacts/refactor.md",
    instruction:
      "Apply code-generation-discipline. Refactor only after green, keep behavior stable, apply selected language-specific guides as secondary checklists, keep required Korean code comments, and summarize changed structure.",
  },
  { id: "gates", artifact: "artifacts/gate-results.json", instruction: "Run build, typecheck, lint, tests, and context lint according to the active profile. Docs-only changes must still run context lint. Save structured JSON with a top-level passed boolean and a results array." },
  { id: "multi-review", artifact: "artifacts/multi-review.md", instruction: "Run 2+ independent reviewer agents. Each reviewer uses code-reviewer.md as basis and outputs an independent verdict: approve or request-changes. Any request-changes from any reviewer makes the overall verdict request-changes. Default reviewers are Codex sub-agents; Claude/Gemini are optional when available. Provider failure falls back to Codex sub-agents only. Record per-reviewer verdicts and write a single overall verdict line." },
  {
    id: "fix-loop",
    artifact: "artifacts/fix-loop.md",
    instruction:
      "Apply code-generation-discipline while fixing review/gate failures, add required Korean code comments, apply selected language-specific guides as secondary checklists, or record that no fixes were required.",
  },
  {
    id: "architecture-review",
    artifact: "artifacts/architecture-review.md",
    instruction:
      "Review implemented code against domain decisions and DDD/Clean Architecture. Record verdict: approve or verdict: request-changes with violations and required refactors.",
  },
  { id: "commit", artifact: "artifacts/commit.md", instruction: "Commit the verified slice and record the commit hash." },
  {
    id: "push-pr",
    artifact: "artifacts/push-pr.md",
    instruction:
      "Push the branch or open a PR against profile.pr.target_branch; for release-first profiles, verify the target is the active release/* branch. Record the remote reference.",
  },
  { id: "pr-watch", artifact: "artifacts/pr-watch.md", instruction: "Poll PR checks and review threads; record status: green, status: comments, status: ci-failed, status: pending, status: merged, status: closed, or status: error with PR URL." },
  { id: "pr-comment-fix", artifact: "artifacts/pr-comment-fix.md", instruction: "Resolve actionable PR review comments, commit and push fixes, resolve the corresponding GitHub review threads, or record that no comments are pending." },
  { id: "pr-ci-fix", artifact: "artifacts/pr-ci-fix.md", instruction: "Fix failed PR checks, commit and push fixes, or record that checks are green." },
  { id: "merge-approval", artifact: "artifacts/merge-approval.md", instruction: "Ask for explicit user approval before merge. Record verdict: approve only after approval." },
  { id: "merge", artifact: "artifacts/merge.md", instruction: "Merge the PR only after approvals, resolved comments, and green checks; record merge SHA or URL." },
  { id: "handoff", artifact: "artifacts/handoff.md", instruction: "Write final handoff with decisions, risks, files, and remaining work." },
];

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
  const stages = PHASES.map(
    (phase) => {
      const markers = (phase.required_markers ?? [])
        .map((marker) => `      - ${JSON.stringify(marker)}`)
        .join("\n");
      const markerBlock = markers ? `\n    required_markers:\n${markers}` : "";
      return `  - id: ${phase.id}\n    artifact: ${phase.artifact}${markerBlock}\n    instruction: ${JSON.stringify(phase.instruction)}`;
    },
  ).join("\n");
  return `id: full-feature\nmode: cli-enforced\nstages:\n${stages}\n`;
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
In a git repo, \`${AGENT_FLOW_COMMAND} run "<task>"\` starts the run inside \`.agent-flow/worktrees/feat-<slug>/\` on branch \`feat/<slug>\` without switching the leader branch.

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
- Keep \`.agent-flow/worktrees/feat-<slug>/.agent-flow/runs/<workflow>/<run-id>/\` as internal state for git projects; expose it only for status, debugging, or artifact inspection.
- On a new session, always check \`${AGENT_FLOW_COMMAND} status\` first and continue from that result.
- After a phase writes its artifact, run the \`next_command\` printed by status or the current phase output.
- If the workflow pauses for design or slice review, summarize the relevant artifact and wait for user approval before continuing.
- Apply code-generation-discipline for code generation and modification phases. Language-specific guide and comment rules follow the code-generation-discipline Before Starting checklist.
`;
}

function fullFeatureSkillMarkdown() {
  return `# Full Feature Workflow\n\nUse this skill for feature work in this project.\n\nAlways drive progress through the runner output. Run \`${AGENT_FLOW_COMMAND} status\`, then execute the printed \`next_command\` exactly.\n\nDo not skip phases. If existing docs satisfy a phase, write the required artifact and reference those docs. If a gate, review, PR comment, or PR check fails, complete the matching fix phase and push again before merge/handoff.\n\nApply \`code-generation-discipline\` during code phases. Language-specific guide and comment rules follow the \`code-generation-discipline\` Before Starting checklist.\n`;
}

function domainGrillSkillMarkdown() {
  return `# Domain Grill\n\nUse during the full-feature domain-grill phase. This phase uses the internal grill-with-docs discipline.\n\nRules:\n\n- Ask one question at a time and provide a recommended answer.\n- If code, CONTEXT.md, CONTEXT-MAP.md, or ADRs can answer the question, inspect those sources instead of asking.\n- Challenge terminology conflicts against CONTEXT.md and ADRs immediately.\n- Stress-test domain relationships with concrete edge-case scenarios.\n- Cross-check user claims against code before treating them as facts.\n- Update CONTEXT.md inline when a domain term is resolved; keep implementation details out.\n- Keep expanded glossary/rationale in .Codex/rules/context/ instead of hot CONTEXT.md.\n- Create CONTEXT.md, CONTEXT-MAP.md, and ADR files lazily; only write them when there is resolved content.\n- Offer ADRs only for hard-to-reverse, surprising trade-off decisions.\n- Do not mark the artifact complete while unresolved questions remain.\n\nArtifact template:\n\n# Domain Grill\n\n## Goal\n\n## Resolved Decisions\n\n## Domain Map\n\n## Open Questions\n\n## Terms Defined / Updated in CONTEXT.md\n\n## ADRs Created\n\n## Risky Assumptions\n\n## Existing Sources Checked\n\n## Completion Gate\n\ngrill-with-docs: complete\nshared_understanding: reached\ncontext_docs_checked: true\ncontext_docs_updated: true|not_needed\n`;
}

function productBriefSkillMarkdown() {
  return `# Product Brief\n\nUse during the full-feature product-brief phase.\n\nAsk YC-style forcing questions before implementation:\n\n1. Demand Reality: what behavior proves people want this?\n2. Status Quo: how do they solve it today?\n3. Desperate Specificity: who is the most painful target user?\n4. Narrowest Wedge: what is the smallest version worth using now?\n5. Observation: what concrete user behavior was observed?\n6. Future Fit: why is now the right time?\n\nArtifact template:\n\n# Product Brief\n\n## Mode\nstartup | builder | internal\n\n## Demand Evidence\n\n## Status Quo\n\n## Target User\n\n## Narrowest Wedge\n\n## Observed Behavior\n\n## Why Now\n\n## Cut List\n\n## Assignment\n\n## Decision\nbuild | defer | cut\n`;
}

function planReviewerSkillMarkdown() {
  return `# Plan Reviewer\n\nUse during the full-feature plan-review phase.\n\nReview only. Do not rewrite the plan.\n\nCheck:\n\n- Missing data collection steps.\n- Missing validation steps.\n- Wrong implementation order.\n- Oversized slices that should be split.\n- Missing state/storage steps.\n- Test coverage gaps.\n- Architecture risks before coding.\n\nArtifact template:\n\n# Plan Review\n\nverdict: approve | request-changes\n\n## Scope Checked\n\n## Missing Steps\n\n## Wrong Order\n\n## Oversized Slices\n\n## Validation Gaps\n\n## Data/State Gaps\n\n## Architecture Risks\n\n## Required Changes\n\n## Approval Notes\n`;
}

function dddCleanArchitectureSkillMarkdown() {
  return `# DDD Clean Architecture\n\nUse during full-feature ddd-design and architecture-review phases.\n\nDefault architecture is data / domain / presentation with optional shared.\n\nLayer rules:\n\n- domain owns entities, value objects, aggregates, use cases, repository interfaces, domain services, events, errors, policies, and specifications.\n- data owns repository implementations, API/DB clients, persistence models, mappers, and external integrations.\n- presentation owns controllers, routes, components, presenters, view models, and external input handling.\n- shared is optional and must contain only domain-free primitives such as Result, IDs, time, and common errors.\n\nDependency rules:\n\n- domain must not import data or presentation.\n- presentation calls domain use cases.\n- data implements domain repository interfaces.\n- presentation must not call data directly.\n- repository pattern uses interfaces in domain and implementations in data.\n\nDesign artifact must identify domain core modules and data / domain / presentation boundaries.\n`;
}

function architectureReviewerSkillMarkdown() {
  return `# Architecture Reviewer\n\nUse during the full-feature architecture-review phase.\n\nReview implemented code against domain decisions and DDD/Clean Architecture.\n\nArtifact template:\n\n# Architecture Review\n\nverdict: approve | request-changes\n\n## Domain Alignment\n\n## Layer Violations\n\n## Repository Boundary Issues\n\n## Dependency Direction Issues\n\n## Required Refactors\n\n## Approved Exceptions\n`;
}

function pushWatchSkillMarkdown() {
  return `# Push Watch\n\nUse this skill after local verification is complete and the branch is ready to publish.\n\nRun:\n\n\`\`\`bash\n${AGENT_FLOW_COMMAND} run push-watch\n\`\`\`\n\nFlow:\n\n1. Sanity check the branch and working tree.\n2. Commit and push the current branch.\n3. Open or record the pull request.\n4. Watch PR checks and review threads.\n5. Route failures through \`pr-comment-fix\` or \`pr-ci-fix\`; comment fixes must also resolve the corresponding GitHub review threads.\n6. Push again and return to \`pr-watch\`.\n7. When checks and comments are green, route to \`merge\`.\n\nRules:\n\n- Protected branches are blocked: main, master, develop.\n- Record PR watch state with \`status: green\`, \`status: comments\`, \`status: ci-failed\`, or \`status: pending\`.\n- merge requires explicit approval. Do not merge unattended.\n`;
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

function claudeHooksSettings() {
  return {
    hooks: {
      PreToolUse: [
        {
          matcher: "Bash",
          hooks: [
            { type: "command", command: "scripts/hooks/guard-worktree.sh" },
            { type: "command", command: "scripts/hooks/guard-protected-branch.sh" },
          ],
        },
      ],
      Stop: [
        {
          matcher: "",
          hooks: [{ type: "command", command: "scripts/hooks/show-phase-status.sh" }],
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
  const desired = claudeHooksSettings().hooks;
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
          if (!existing.hooks.some((h) => h.command === hook.command)) {
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
- Apply \`code-generation-discipline\` during red, green, refactor, and fix-loop phases. Language-specific guide and comment rules follow the \`code-generation-discipline\` Before Starting checklist.
- If review or QA fails, return to the fix phase before continuing.
- The gates->fix-loop->gates loop re-verifies after every fix. multi-review approve skips fix-loop; request-changes routes through fix-loop->gates.

Document size rules:

- \`CONTEXT.md\`, grill-with-docs outputs, compact domain maps, and long planning docs must stay under 200 lines each.
- If a source doc grows past 200 lines, create or refresh a matching \`*-summary.md\` under \`.Codex/rules/\` and use that summary as agent context.
- Preserve the original long doc only as reference; do not load it as hot context unless the current phase needs a specific section.
- Artifacts must link to long docs by repo-relative path and summarize only the needed decision, not paste the full content.

Context rules:

- Artifacts and manifests must use repo-relative paths; local absolute paths are forbidden.
- Do not paste full docs or raw logs into artifacts. Summarize and link by relative path.
- \`CONTEXT.md\` is hot context only and must stay under 200 lines.
- Current and future vocabulary must stay separated.
- Follow the phase context map in \`.Codex/rules/context/\` for phase-specific context loading.
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

  console.error("usage: agent-flow-kit install [--without-graphify] | run <install|start|status|next|advance|push-watch|push-watch-tick>");
  process.exit(1);
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
}
