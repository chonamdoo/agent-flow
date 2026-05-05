#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import process from "node:process";

const command = process.argv[2];

function installProject() {
  const root = process.cwd();
  const agentFlowDir = path.join(root, ".agent-flow");
  const profile = detectProfile(root);

  for (const name of ["runs", "state", "handoffs", "team", "worktrees", "workflows", "skills", "prompts", "rules", "bootstrap"]) {
    fs.mkdirSync(path.join(agentFlowDir, name), { recursive: true });
  }

  fs.mkdirSync(path.join(agentFlowDir, "skills", "full-feature-workflow"), { recursive: true });

  const payload = {
    install_scope: "project",
    profile,
    root,
    installed_at: new Date().toISOString(),
  };

  writeFileIfMissing(path.join(agentFlowDir, "workflows", "full-feature.yaml"), fullFeatureWorkflowYaml());
  writeFileIfMissing(
    path.join(agentFlowDir, "skills", "full-feature-workflow", "SKILL.md"),
    fullFeatureSkillMarkdown(),
  );
  for (const phase of PHASES) {
    writeFileIfMissing(
      path.join(agentFlowDir, "prompts", `${phase.id}.md`),
      phasePrompt(phase),
    );
  }
  writeFileIfMissing(path.join(agentFlowDir, "rules", "workflow-contract.md"), workflowContract());
  writeFileIfMissing(path.join(agentFlowDir, "bootstrap", "AGENTS.md"), bootstrapMarkdown("AGENTS.md"));
  writeFileIfMissing(path.join(agentFlowDir, "bootstrap", "CLAUDE.md"), bootstrapMarkdown("CLAUDE.md"));
  writeFileIfMissing(path.join(agentFlowDir, "bootstrap", "GEMINI.md"), bootstrapMarkdown("GEMINI.md"));
  upsertBootstrapBlock(path.join(root, "AGENTS.md"), "AGENTS.md");
  upsertBootstrapBlock(path.join(root, "CLAUDE.md"), "CLAUDE.md");
  upsertBootstrapBlock(path.join(root, "GEMINI.md"), "GEMINI.md");

  fs.writeFileSync(path.join(agentFlowDir, "kit.json"), `${JSON.stringify(payload, null, 2)}\n`, "utf8");
  console.log(`agent-flow installed profile=${profile}`);
}

function runWorkflowCommand(args) {
  const subcommand = args[0];
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
    const root = process.cwd();
    assertInstalled(root);
    const runDir = path.join(root, ".agent-flow", "runs", workflow, runId);
    if (fs.existsSync(runDir)) {
      throw new Error(`run already exists: ${runId}`);
    }
    fs.mkdirSync(path.join(runDir, "artifacts"), { recursive: true });
    fs.mkdirSync(path.join(runDir, "logs"), { recursive: true });
    const state = {
      run_id: runId,
      workflow,
      task,
      phase_index: 0,
      phase: PHASES[0].id,
      status: "running",
      run_dir: runDir,
      started_at: new Date().toISOString(),
    };
    writeJson(path.join(runDir, "manifest.json"), state);
    writeJson(currentRunPath(root), state);
    printNext(state);
    return;
  }

  if (subcommand === "status") {
    const state = readCurrentRun(process.cwd());
    console.log(`${state.workflow} ${state.run_id} ${state.status} phase=${state.phase}`);
    return;
  }

  if (subcommand === "next") {
    printNext(readCurrentRun(process.cwd()));
    return;
  }

  if (subcommand === "advance") {
    const root = process.cwd();
    const state = readCurrentRun(root);
    if (state.status === "complete" || state.phase === "complete") {
      console.log(`workflow already complete: ${state.run_id}`);
      return;
    }
    const phase = PHASES[state.phase_index];
    const artifact = path.join(state.run_dir, phase.artifact);
    if (!fs.existsSync(artifact)) {
      throw new Error(`blocked: missing artifact ${artifact}`);
    }
    const nextIndex = nextPhaseIndex(state, phase, artifact);
    const nextPhase = PHASES[nextIndex];
    const nextState = {
      ...state,
      phase_index: nextIndex,
      phase: nextPhase?.id ?? "complete",
      status: nextPhase ? "running" : "complete",
      updated_at: new Date().toISOString(),
    };
    writeJson(path.join(state.run_dir, "manifest.json"), nextState);
    writeJson(currentRunPath(root), nextState);
    if (nextPhase) {
      printNext(nextState);
    } else {
      console.log(`workflow complete: ${state.run_id}`);
    }
    return;
  }

  throw new Error("usage: agent-flow-kit run <start|status|next|advance>");
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
    return "node";
  }
  if (fs.existsSync(path.join(rootDir, "pyproject.toml"))) {
    return "python";
  }
  return "generic";
}

function readCurrentRun(root) {
  const pathName = currentRunPath(root);
  if (!fs.existsSync(pathName)) {
    throw new Error("no active run. start one with: agent-flow-kit run start --task <task>");
  }
  return JSON.parse(fs.readFileSync(pathName, "utf8"));
}

function assertInstalled(root) {
  const required = [
    path.join(root, ".agent-flow", "kit.json"),
    path.join(root, ".agent-flow", "workflows", "full-feature.yaml"),
    path.join(root, ".agent-flow", "skills", "full-feature-workflow", "SKILL.md"),
    path.join(root, ".agent-flow", "bootstrap", "AGENTS.md"),
    path.join(root, ".agent-flow", "bootstrap", "CLAUDE.md"),
    path.join(root, ".agent-flow", "bootstrap", "GEMINI.md"),
  ];
  const missing = required.filter((pathName) => !fs.existsSync(pathName));
  if (missing.length > 0) {
    throw new Error(`agent-flow is not installed. run: agent-flow-kit install`);
  }
}

function currentRunPath(root) {
  return path.join(root, ".agent-flow", "state", "current-run.json");
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

function writeFileIfMissing(pathName, content) {
  fs.mkdirSync(path.dirname(pathName), { recursive: true });
  if (!fs.existsSync(pathName)) {
    fs.writeFileSync(pathName, content, "utf8");
  }
}

function nextPhaseIndex(state, phase, artifact) {
  if (phase.id === "pr-watch") {
    const status = readArtifactStatus(artifact);
    if (status === "green") {
      return phaseIndex("merge");
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
    throw new Error("blocked: pr-watch artifact must include status: green, comments, ci-failed, or pending");
  }
  if (phase.id === "pr-comment-fix" || phase.id === "pr-ci-fix") {
    return phaseIndex("pr-watch");
  }
  return state.phase_index + 1;
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
  const match = content.match(/^status:\s*([a-z-]+)\s*$/im);
  return match?.[1];
}

function upsertBootstrapBlock(pathName, label) {
  const start = "<!-- agent-flow:start -->";
  const end = "<!-- agent-flow:end -->";
  const block = `${start}
## Agent Flow

Before feature work, run:

\`\`\`bash
agent-flow-kit run start --task "<task>"
agent-flow-kit run next
\`\`\`

Follow the CLI output exactly. Do not manually skip phases; use \`agent-flow-kit run advance\` only after the required artifact exists.

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
agent-flow-kit run start --task "<task>"
agent-flow-kit run next
\`\`\`

Follow the CLI output exactly. Do not manually skip phases; use \`agent-flow-kit run advance\` only after the required artifact exists.
`;
}

function newRunId() {
  const stamp = new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
  return `${stamp}-${Math.random().toString(16).slice(2, 10)}`;
}

const PHASES = [
  { id: "prd", artifact: "artifacts/prd.md", instruction: "Write the PRD before planning slices." },
  { id: "slice-plan", artifact: "artifacts/slice-plan.md", instruction: "Break the PRD into independently shippable slices." },
  { id: "worktree", artifact: "artifacts/worktree.md", instruction: "Create or record the dedicated branch/worktree for this slice." },
  { id: "run-start", artifact: "artifacts/run-start.md", instruction: "Record the workflow run setup and selected provider." },
  { id: "red", artifact: "artifacts/red.log", instruction: "Write failing tests first and save the failure output." },
  { id: "green", artifact: "artifacts/green.log", instruction: "Implement the minimum change and save passing test output." },
  { id: "refactor", artifact: "artifacts/refactor.md", instruction: "Refactor only after green and summarize changed structure." },
  { id: "gates", artifact: "gate-results.json", instruction: "Run project gates and save structured results." },
  { id: "multi-review", artifact: "artifacts/multi-review.md", instruction: "Run reviewer agents and record approve/request-changes results." },
  { id: "fix-loop", artifact: "artifacts/fix-loop.md", instruction: "Apply review/gate fixes or record that no fixes were required." },
  { id: "commit", artifact: "artifacts/commit.md", instruction: "Commit the verified slice and record the commit hash." },
  { id: "push-pr", artifact: "artifacts/push-pr.md", instruction: "Push the branch or open a PR and record the remote reference." },
  { id: "pr-watch", artifact: "artifacts/pr-watch.md", instruction: "Poll PR checks and review threads; record status: green, status: comments, status: ci-failed, or status: pending with PR URL." },
  { id: "pr-comment-fix", artifact: "artifacts/pr-comment-fix.md", instruction: "Resolve actionable PR review comments, commit and push fixes, or record that no comments are pending." },
  { id: "pr-ci-fix", artifact: "artifacts/pr-ci-fix.md", instruction: "Fix failed PR checks, commit and push fixes, or record that checks are green." },
  { id: "merge", artifact: "artifacts/merge.md", instruction: "Merge the PR only after approvals, resolved comments, and green checks; record merge SHA or URL." },
  { id: "handoff", artifact: "artifacts/handoff.md", instruction: "Write final handoff with decisions, risks, files, and remaining work." },
];

function fullFeatureWorkflowYaml() {
  const stages = PHASES.map(
    (phase) => `  - id: ${phase.id}\n    artifact: ${phase.artifact}\n    instruction: ${JSON.stringify(phase.instruction)}`,
  ).join("\n");
  return `id: full-feature\nmode: cli-enforced\nstages:\n${stages}\n`;
}

function fullFeatureSkillMarkdown() {
  return `# Full Feature Workflow\n\nUse this skill for feature work in this project.\n\nAlways drive progress through:\n\n\`\`\`bash\nagent-flow-kit run next\n\`\`\`\n\nCanonical order:\n\n${PHASES.map((phase, index) => `${index + 1}. ${phase.id}`).join("\n")}\n\nDo not skip phases. If a gate, review, PR comment, or PR check fails, complete the matching fix phase and push again before merge/handoff.\n`;
}

function phasePrompt(phase) {
  return `# ${phase.id}\n\n${phase.instruction}\n\nSave the required artifact before running:\n\n\`\`\`bash\nagent-flow-kit run advance\n\`\`\`\n`;
}

function workflowContract() {
  return `# Workflow Contract\n\nThe workflow runner is the source of truth for phase order. Agents may read skills and prompts, but must use \`agent-flow-kit run next\` and \`agent-flow-kit run advance\` to move through the workflow.\n`;
}

try {
  if (command === "install") {
    installProject();
    process.exit(0);
  }

  if (command === "run") {
    runWorkflowCommand(process.argv.slice(3));
    process.exit(0);
  }

  console.error("usage: agent-flow-kit install | run <start|status|next|advance>");
  process.exit(1);
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
}
