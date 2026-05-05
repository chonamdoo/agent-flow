#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import process from "node:process";

const command = process.argv[2];
const AGENT_FLOW_COMMAND = "npx github:chonamdoo/agent-flow";

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

  writeManagedFile(path.join(agentFlowDir, "workflows", "full-feature.yaml"), fullFeatureWorkflowYaml());
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
    const startedAt = new Date().toISOString();
    const state = {
      run_id: runId,
      workflow,
      task,
      phase_index: 0,
      phase: PHASES[0].id,
      status: "running",
      run_dir: runDir,
      started_at: startedAt,
      phase_entered_at: startedAt,
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
    assertFreshArtifact(state, phase, artifact);
    const nextIndex = nextPhaseIndex(state, phase, artifact);
    const nextPhase = PHASES[nextIndex];
    const transitionedAt = new Date().toISOString();
    const nextState = {
      ...state,
      phase_index: nextIndex,
      phase: nextPhase?.id ?? "complete",
      status: nextPhase ? "running" : "complete",
      updated_at: transitionedAt,
      phase_entered_at: transitionedAt,
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
  return normalizeRunState(root, JSON.parse(fs.readFileSync(pathName, "utf8")));
}

function assertInstalled(root) {
  const required = [
    path.join(root, ".agent-flow", "kit.json"),
    path.join(root, ".agent-flow", "workflows", "full-feature.yaml"),
    path.join(root, ".agent-flow", "skills", "full-feature-workflow", "SKILL.md"),
    ...PHASES.map((phase) => path.join(root, ".agent-flow", "prompts", `${phase.id}.md`)),
    path.join(root, ".agent-flow", "skills", "domain-grill", "SKILL.md"),
    path.join(root, ".agent-flow", "skills", "product-brief", "SKILL.md"),
    path.join(root, ".agent-flow", "skills", "plan-reviewer", "SKILL.md"),
    path.join(root, ".agent-flow", "skills", "ddd-clean-architecture", "SKILL.md"),
    path.join(root, ".agent-flow", "skills", "architecture-reviewer", "SKILL.md"),
    path.join(root, ".agent-flow", "bootstrap", "AGENTS.md"),
    path.join(root, ".agent-flow", "bootstrap", "CLAUDE.md"),
    path.join(root, ".agent-flow", "bootstrap", "GEMINI.md"),
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
  writeJson(path.join(state.run_dir, "manifest.json"), normalized);
  writeJson(currentRunPath(root), normalized);
  return normalized;
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

function writeManagedFile(pathName, content) {
  fs.mkdirSync(path.dirname(pathName), { recursive: true });
  fs.writeFileSync(pathName, content, "utf8");
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
  if (phase.id === "architecture-review") {
    const verdict = readArtifactVerdict(artifact);
    if (verdict === "approve") {
      return state.phase_index + 1;
    }
    if (verdict === "request-changes") {
      return phaseIndex("refactor");
    }
    throw new Error("blocked: architecture-review artifact must include verdict: approve or verdict: request-changes");
  }
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

function readArtifactVerdict(pathName) {
  const content = fs.readFileSync(pathName, "utf8");
  const match = content.match(/^verdict:\s*([a-z-]+)\s*$/im);
  return match?.[1]?.toLowerCase();
}

function upsertBootstrapBlock(pathName, label) {
  const start = "<!-- agent-flow:start -->";
  const end = "<!-- agent-flow:end -->";
  const block = `${start}
## Agent Flow

Before feature work, run:

\`\`\`bash
${AGENT_FLOW_COMMAND} run start --task "<task>"
${AGENT_FLOW_COMMAND} run next
\`\`\`

Follow the CLI output exactly. Do not manually skip phases; use \`${AGENT_FLOW_COMMAND} run advance\` only after the required artifact exists.

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
${AGENT_FLOW_COMMAND} run start --task "<task>"
${AGENT_FLOW_COMMAND} run next
\`\`\`

Follow the CLI output exactly. Do not manually skip phases; use \`${AGENT_FLOW_COMMAND} run advance\` only after the required artifact exists.
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
    instruction:
      "Interview one question at a time, resolve domain decisions, and record decisions, open questions, terms, assumptions, and sources checked.",
  },
  {
    id: "domain-map",
    artifact: "artifacts/domain-map.md",
    instruction:
      "Update or reference CONTEXT.md with glossary, bounded contexts, ubiquitous language, and domain decisions from domain-grill.",
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
  { id: "worktree", artifact: "artifacts/worktree.md", instruction: "Create or record the dedicated branch/worktree for this slice." },
  { id: "run-start", artifact: "artifacts/run-start.md", instruction: "Record the workflow run setup and selected provider." },
  { id: "red", artifact: "artifacts/red.log", instruction: "Write failing tests first and save the failure output." },
  { id: "green", artifact: "artifacts/green.log", instruction: "Implement the minimum change and save passing test output." },
  { id: "refactor", artifact: "artifacts/refactor.md", instruction: "Refactor only after green and summarize changed structure." },
  { id: "gates", artifact: "gate-results.json", instruction: "Run project gates and save structured results." },
  { id: "multi-review", artifact: "artifacts/multi-review.md", instruction: "Run reviewer agents and record approve/request-changes results." },
  { id: "fix-loop", artifact: "artifacts/fix-loop.md", instruction: "Apply review/gate fixes or record that no fixes were required." },
  {
    id: "architecture-review",
    artifact: "artifacts/architecture-review.md",
    instruction:
      "Review implemented code against domain decisions and DDD/Clean Architecture. Record verdict: approve or verdict: request-changes with violations and required refactors.",
  },
  { id: "commit", artifact: "artifacts/commit.md", instruction: "Commit the verified slice and record the commit hash." },
  { id: "push-pr", artifact: "artifacts/push-pr.md", instruction: "Push the branch or open a PR and record the remote reference." },
  { id: "pr-watch", artifact: "artifacts/pr-watch.md", instruction: "Poll PR checks and review threads; record status: green, status: comments, status: ci-failed, or status: pending with PR URL." },
  { id: "pr-comment-fix", artifact: "artifacts/pr-comment-fix.md", instruction: "Resolve actionable PR review comments, commit and push fixes, or record that no comments are pending." },
  { id: "pr-ci-fix", artifact: "artifacts/pr-ci-fix.md", instruction: "Fix failed PR checks, commit and push fixes, or record that checks are green." },
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
    (phase) => `  - id: ${phase.id}\n    artifact: ${phase.artifact}\n    instruction: ${JSON.stringify(phase.instruction)}`,
  ).join("\n");
  return `id: full-feature\nmode: cli-enforced\nstages:\n${stages}\n`;
}

function fullFeatureSkillMarkdown() {
  return `# Full Feature Workflow\n\nUse this skill for feature work in this project.\n\nAlways drive progress through:\n\n\`\`\`bash\n${AGENT_FLOW_COMMAND} run next\n\`\`\`\n\nCanonical order:\n\n${PHASES.map((phase, index) => `${index + 1}. ${phase.id}`).join("\n")}\n\nDo not skip phases. If existing docs satisfy a phase, write the required artifact and reference those docs. If a gate, review, PR comment, or PR check fails, complete the matching fix phase and push again before merge/handoff.\n\nCoding rule:\n\n- Code comments are required when intent is not obvious, and every code comment must be written in Korean.\n`;
}

function domainGrillSkillMarkdown() {
  return `# Domain Grill\n\nUse during the full-feature domain-grill phase.\n\nRules:\n\n- Ask one question at a time.\n- Provide a recommended answer for every question.\n- Walk each branch of the design tree until decisions are explicit.\n- If a question can be answered from code or docs, inspect those sources instead of asking.\n- Challenge fuzzy domain terms against CONTEXT.md and ADRs when present.\n\nArtifact template:\n\n# Domain Grill\n\n## Goal\n\n## Resolved Decisions\n\n## Open Questions\n\n## Terms To Define\n\n## Risky Assumptions\n\n## Existing Sources Checked\n`;
}

function productBriefSkillMarkdown() {
  return `# Product Brief\n\nUse during the full-feature product-brief phase.\n\nAsk YC-style forcing questions before implementation:\n\n1. Demand Reality: what behavior proves people want this?\n2. Status Quo: how do they solve it today?\n3. Desperate Specificity: who is the most painful target user?\n4. Narrowest Wedge: what is the smallest version worth using now?\n5. Observation: what concrete user behavior was observed?\n6. Future Fit: why is now the right time?\n\nArtifact template:\n\n# Product Brief\n\n## Mode\nstartup | builder | internal\n\n## Demand Evidence\n\n## Status Quo\n\n## Target User\n\n## Narrowest Wedge\n\n## Observed Behavior\n\n## Why Now\n\n## Cut List\n\n## Assignment\n\n## Decision\nbuild | defer | cut\n`;
}

function planReviewerSkillMarkdown() {
  return `# Plan Reviewer\n\nUse during the full-feature plan-review phase.\n\nReview only. Do not rewrite the plan.\n\nCheck:\n\n- Missing data collection steps.\n- Missing validation steps.\n- Wrong implementation order.\n- Oversized slices that should be split.\n- Missing state/storage steps.\n- Test coverage gaps.\n- Architecture risks before coding.\n\nArtifact template:\n\n# Plan Review\n\nverdict: approve | request-changes\n\n## Scope Checked\n\n## Missing Steps\n\n## Wrong Order\n\n## Oversized Slices\n\n## Validation Gaps\n\n## Data/State Gaps\n\n## Architecture Risks\n\n## Required Changes\n\n## Approval Notes\n`;
}

function dddCleanArchitectureSkillMarkdown() {
  return `# DDD Clean Architecture\n\nUse during full-feature ddd-design and architecture-review phases.\n\nDefault architecture is data / domain / presentation with optional shared.\n\nLayer rules:\n\n- domain owns entities, value objects, aggregates, use cases, repository interfaces, domain services, events, errors, policies, and specifications.\n- data owns repository implementations, API/DB clients, persistence models, mappers, and external integrations.\n- presentation owns controllers, routes, components, presenters, view models, and external input handling.\n- shared is optional and must contain only domain-free primitives such as Result, IDs, time, and common errors.\n\nDependency rules:\n\n- domain must not import data or presentation.\n- presentation calls domain use cases.\n- data implements domain repository interfaces.\n- presentation must not call data directly.\n- repository pattern uses interfaces in domain and implementations in data.\n\nCoding rule:\n\n- Code comments are required when intent is not obvious, and every code comment must be written in Korean.\n\nDesign artifact must identify domain core modules and data / domain / presentation boundaries.\n`;
}

function architectureReviewerSkillMarkdown() {
  return `# Architecture Reviewer\n\nUse during the full-feature architecture-review phase.\n\nReview implemented code against domain decisions and DDD/Clean Architecture.\n\nArtifact template:\n\n# Architecture Review\n\nverdict: approve | request-changes\n\n## Domain Alignment\n\n## Layer Violations\n\n## Repository Boundary Issues\n\n## Dependency Direction Issues\n\n## Required Refactors\n\n## Approved Exceptions\n`;
}

function phasePrompt(phase) {
  return `# ${phase.id}\n\n${phase.instruction}\n\nSave the required artifact before running:\n\n\`\`\`bash\n${AGENT_FLOW_COMMAND} run advance\n\`\`\`\n`;
}

function workflowContract() {
  return `# Workflow Contract\n\nThe workflow runner is the source of truth for phase order. Agents may read skills and prompts, but must use \`${AGENT_FLOW_COMMAND} run next\` and \`${AGENT_FLOW_COMMAND} run advance\` to move through the workflow.\n`;
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
