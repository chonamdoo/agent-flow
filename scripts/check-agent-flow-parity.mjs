#!/usr/bin/env node

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";

const SOURCE_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const HOME = process.env.HOME || process.env.USERPROFILE || "";
const SOURCE_IS_MANAGED_WORKTREE = resolveManagedWorktreeRoot(SOURCE_ROOT) !== null;
const CHECK_INSTALLED_COPY = !SOURCE_IS_MANAGED_WORKTREE;
const INSTALL_ROOT = resolveInstalledRoot(process.cwd()) ?? SOURCE_ROOT;
const failures = [];
const missingFiles = new Set();

function read(rel) {
  return fs.readFileSync(absPath(rel), "utf8");
}

function readIfExists(rel) {
  const abs = absPath(rel);
  if (!fs.existsSync(abs)) {
    recordMissingFile(rel);
    return null;
  }
  return fs.readFileSync(abs, "utf8");
}

function assertFile(rel) {
  if (!fs.existsSync(absPath(rel))) {
    recordMissingFile(rel);
  }
}

function absPath(rel) {
  return path.join(rootFor(rel), rel);
}

function rootFor(rel) {
  return rel.startsWith(".agent-flow/") ? INSTALL_ROOT : SOURCE_ROOT;
}

function recordMissingFile(rel) {
  if (!missingFiles.has(rel)) {
    // 누락 파일도 집계해서 한 번에 보고해야 parity drift 원인을 놓치지 않는다.
    missingFiles.add(rel);
    failures.push(`missing file: ${rel}`);
  }
}

function assertContains(rel, needle) {
  const text = readIfExists(rel);
  if (text === null) return;
  if (!text.includes(needle)) {
    failures.push(`${rel} missing ${JSON.stringify(needle)}`);
  }
}

function assertNotContains(rel, needle) {
  const text = readIfExists(rel);
  if (text === null) return;
  if (text.includes(needle)) {
    failures.push(`${rel} still contains ${JSON.stringify(needle)}`);
  }
}

function assertSame(a, b) {
  const aText = readIfExists(a);
  const bText = readIfExists(b);
  if (aText === null || bText === null) return;
  if (aText !== bText) {
    failures.push(`${a} differs from ${b}`);
  }
}

function assertSameBodyAfterTitle(a, b) {
  const aText = readIfExists(a);
  const bText = readIfExists(b);
  if (aText === null || bText === null) return;
  const body = (text) => text.split(/\r?\n/).slice(1).join("\n");
  if (body(aText) !== body(bText)) {
    failures.push(`${a} body differs from ${b}`);
  }
}

function yamlFileNames(rel) {
  const dir = absPath(rel);
  if (!fs.existsSync(dir)) {
    recordMissingFile(rel);
    return [];
  }
  return fs.readdirSync(dir)
    .filter((entry) => entry.endsWith(".yaml"))
    .sort();
}

function assertSameYamlFileSet(sourceDir, otherDir) {
  const sourceFiles = yamlFileNames(sourceDir);
  const otherFiles = yamlFileNames(otherDir);
  const sourceSet = new Set(sourceFiles);
  const otherSet = new Set(otherFiles);
  for (const file of sourceFiles) {
    if (!otherSet.has(file)) {
      failures.push(`${otherDir} missing ${file} from ${sourceDir}`);
    }
  }
  for (const file of otherFiles) {
    if (!sourceSet.has(file)) {
      failures.push(`${otherDir} has extra ${file} not in ${sourceDir}`);
    }
  }
}

function assertSameRelativeFileSet(sourceDir, otherDir) {
  const sourceFiles = recursiveFiles(sourceDir).map((file) => path.relative(sourceDir, file)).sort();
  const otherFiles = recursiveFiles(otherDir).map((file) => path.relative(otherDir, file)).sort();
  const sourceSet = new Set(sourceFiles);
  const otherSet = new Set(otherFiles);
  for (const file of sourceFiles) {
    if (!otherSet.has(file)) {
      failures.push(`${otherDir} missing ${file} from ${sourceDir}`);
    }
  }
  for (const file of otherFiles) {
    if (!sourceSet.has(file)) {
      failures.push(`${otherDir} has extra ${file} not in ${sourceDir}`);
    }
  }
}

function recursiveFiles(relDir) {
  const dir = absPath(relDir);
  if (!fs.existsSync(dir)) {
    recordMissingFile(relDir);
    return [];
  }
  const out = [];
  const visit = (current) => {
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      const entryPath = path.join(current, entry.name);
      if (entry.isDirectory()) {
        visit(entryPath);
      } else if (entry.isFile()) {
        out.push(path.relative(rootFor(relDir), entryPath));
      }
    }
  };
  visit(dir);
  return out.sort();
}

const fullFeatureWorkflowCopies = [
  "workflows/full-feature.yaml",
  "src/agent_flow/workflows/full-feature.yaml",
  ...(CHECK_INSTALLED_COPY ? [".agent-flow/workflows/full-feature.yaml"] : []),
];

const exportedWorkflow = workflowExport("full-feature");
if (exportedWorkflow) {
  const exportedPhases = Object.fromEntries(exportedWorkflow.phases.map((phase) => [phase.id, phase]));
  if (exportedWorkflow.id !== "full-feature") {
    failures.push("workflow export full-feature id mismatch");
  }
  if (exportedPhases["domain-grill"]?.artifact !== "artifacts/domain-grill.md") {
    failures.push("workflow export domain-grill artifact mismatch");
  }
  if (exportedPhases["gates"]?.routes?.green !== "comment-authoring") {
    failures.push("workflow export gates green route mismatch");
  }
  if (exportedPhases["comment-authoring"]?.routes?.default !== "multi-review") {
    failures.push("workflow export comment-authoring route mismatch");
  }
  if (exportedPhases["red"]?.artifact !== "artifacts/red.log") {
    failures.push("workflow export red artifact mismatch");
  }
  assertRouteParity(exportedWorkflow);
  assertFixLoopRoundParity(exportedWorkflow);
  assertBackwardFreshArtifactSafety(exportedWorkflow);
  assertCompletionMarkerPrefixParity(exportedWorkflow);
  assertCleanInstallCopiesTemplates();
}

const exportedDefaultWorkflow = workflowExport("default");
if (exportedDefaultWorkflow) {
  const exportedPhases = Object.fromEntries(exportedDefaultWorkflow.phases.map((phase) => [phase.id, phase]));
  if (exportedDefaultWorkflow.id !== "default") {
    failures.push("workflow export default id mismatch");
  }
  if (exportedPhases["pr-watch"]?.routes?.has_comments !== "pr-comment-fix") {
    failures.push("workflow export default pr-watch has_comments route mismatch");
  }
  if (exportedPhases["pr-watch"]?.routes?.ci_failed !== "pr-ci-fix") {
    failures.push("workflow export default pr-watch ci_failed route mismatch");
  }
  if (exportedPhases["pr-comment-fix"]?.routes?.default !== "pr-watch") {
    failures.push("workflow export default pr-comment-fix route mismatch");
  }
  if (exportedPhases["pr-ci-fix"]?.routes?.default !== "pr-watch") {
    failures.push("workflow export default pr-ci-fix route mismatch");
  }
  if (exportedPhases["comment-authoring"]?.routes?.default !== "final-review") {
    failures.push("workflow export default comment-authoring route mismatch");
  }
  assertRouteParity(exportedDefaultWorkflow);
}

// phase 제거가 source/generated copy 중 한 곳에만 반영되는 drift를 막는다.
for (const rel of fullFeatureWorkflowCopies) {
  assertFile(rel);
  assertContains(rel, "id: domain-grill");
  assertContains(rel, "context_docs_updated: true|not_needed");
  assertContains(rel, "skills_checked: true");
  assertContains(rel, "id: comment-authoring");
  assertContains(rel, "comment-authoring: applied");
  assertContains(rel, "comment-checker: checked|unavailable|n/a");
  assertContains(rel, "`n/a` only when the changed diff has no");
  assertContains(rel, "Default reviewers are active-host sub-agents");
  assertContains(rel, "Gemini sub-agent in Gemini");
  assertContains(rel, "reviewer-source: sub-agent");
  assertContains(rel, "close that sub-agent session");
  assertContains(rel, "## Overall");
  assertContains(rel, "verdict: approve");
  assertContains(rel, "verdict: request-changes");
  assertContains(rel, "multi_review: true");
  assertNotContains(rel, "id: domain-map");
  assertNotContains(rel, "grill-me");
}

assertSameYamlFileSet("workflows", "src/agent_flow/workflows");
if (CHECK_INSTALLED_COPY) {
  assertSameYamlFileSet("workflows", ".agent-flow/workflows");
}
for (const entry of fs.readdirSync(path.join(SOURCE_ROOT, "workflows")).sort()) {
  if (!entry.endsWith(".yaml")) continue;
  const source = `workflows/${entry}`;
  assertSame(source, `src/agent_flow/workflows/${entry}`);
  if (CHECK_INSTALLED_COPY) {
    assertSame(source, `.agent-flow/workflows/${entry}`);
  }
}
assertContains("workflows/default.yaml", "active-host reviewer sub-agents");
assertContains("workflows/default.yaml", "Gemini sub-agent in Gemini");
assertContains("workflows/default.yaml", "reviewer-source: sub-agent");
assertContains("workflows/default.yaml", "close that sub-agent session");
assertContains("workflows/default.yaml", "## Overall");
assertContains("workflows/default.yaml", "verdict: approve");
assertContains("workflows/default.yaml", "verdict: request-changes");
assertContains("workflows/default.yaml", "id: comment-authoring");
assertContains("workflows/default.yaml", "`n/a` only when the changed diff has no");
assertContains("workflows/default.yaml", "comment-scope: final-pass-only");
assertContains("skills/code-generation-discipline/SKILL.md", "Write comments only when code alone cannot carry the reason or contract.");
assertNotContains("skills/code-generation-discipline/SKILL.md", "Every new or modified code block must include Korean " + "comments");
if (CHECK_INSTALLED_COPY) {
  assertContains(".agent-flow/prompts/multi-review.md", "Default reviewers are active-host sub-agents");
  assertContains(".agent-flow/prompts/multi-review.md", "skills_checked: true");
  assertContains(".agent-flow/prompts/multi-review.md", "Gemini sub-agent in Gemini");
  assertContains(".agent-flow/prompts/multi-review.md", "reviewer-source: sub-agent");
  assertContains(".agent-flow/prompts/multi-review.md", "close that sub-agent session");
  assertContains(".agent-flow/prompts/multi-review.md", "## Overall");
  assertContains(".agent-flow/prompts/multi-review.md", "verdict: approve");
  assertContains(".agent-flow/prompts/multi-review.md", "verdict: request-changes");
}

// skill source와 설치본이 달라지면 다른 프로젝트로 전파될 때 기준이 갈린다.
if (CHECK_INSTALLED_COPY) {
  assertSame("skills/grill-with-docs/SKILL.md", ".agent-flow/skills/grill-with-docs/SKILL.md");
  assertSame("skills/domain-grill/SKILL.md", ".agent-flow/skills/domain-grill/SKILL.md");
  assertSame("skills/domain-grill/CONTEXT-FORMAT.md", ".agent-flow/skills/domain-grill/CONTEXT-FORMAT.md");
  assertSame("skills/domain-grill/ADR-FORMAT.md", ".agent-flow/skills/domain-grill/ADR-FORMAT.md");
  assertSame("skills/agent-flow/SKILL.md", ".agent-flow/skills/agent-flow/SKILL.md");
  assertSame("skills/code-generation-discipline/SKILL.md", ".agent-flow/skills/code-generation-discipline/SKILL.md");
}

function gateIds(text) {
  const lines = text.split("\n");
  const ids = [];
  let inGates = false;
  for (const line of lines) {
    if (line === "gates:") {
      inGates = true;
      continue;
    }
    if (inGates && line && !line.startsWith(" ")) {
      break;
    }
    if (!inGates) continue;
    const match = line.match(/^\s+- id: (.+)$/);
    if (match) ids.push(match[1].trim());
  }
  return ids;
}

assertSameYamlFileSet("profiles", "src/agent_flow/profiles");
if (CHECK_INSTALLED_COPY) {
  assertSameYamlFileSet("profiles", ".agent-flow/profiles");
}

if (CHECK_INSTALLED_COPY) {
  assertSameRelativeFileSet("templates", ".agent-flow/templates");
  for (const rel of recursiveFiles("templates")) {
    assertSame(rel, `.agent-flow/${rel}`);
  }
}
for (const entry of fs.readdirSync(path.join(SOURCE_ROOT, "profiles")).sort()) {
  if (!entry.endsWith(".yaml")) continue;
  const source = `profiles/${entry}`;
  const packaged = `src/agent_flow/profiles/${entry}`;
  const sourceText = readIfExists(source);
  const packagedText = readIfExists(packaged);
  if (sourceText === null || packagedText === null) continue;
  if (sourceText !== packagedText) {
    failures.push(`${packaged} differs from ${source}`);
  }
  if (CHECK_INSTALLED_COPY) {
    assertSame(source, `.agent-flow/profiles/${entry}`);
  }
  const sourceGates = gateIds(sourceText);
  const packagedGates = gateIds(packagedText);
  if (sourceGates.join("|") !== packagedGates.join("|")) {
    failures.push(`${packaged} gates differ from ${source}`);
  }
  if (sourceGates.includes("context-lint") !== packagedGates.includes("context-lint")) {
    failures.push(`${packaged} context-lint presence differs from ${source}`);
  }
}

// bootstrap은 반복 install 대신 기존 설치된 CLI로 worktree run을 시작해야 한다.
assertSame("bootstrap/AGENTS.md.template", "bootstrap/CLAUDE.md.template");
assertSame("bootstrap/AGENTS.md.template", "bootstrap/GEMINI.md.template");
if (CHECK_INSTALLED_COPY) {
  assertSameBodyAfterTitle(".agent-flow/bootstrap/AGENTS.md", ".agent-flow/bootstrap/CLAUDE.md");
  assertSameBodyAfterTitle(".agent-flow/bootstrap/AGENTS.md", ".agent-flow/bootstrap/GEMINI.md");
}
for (const rel of [
  "bootstrap/AGENTS.md.template",
  "bootstrap/CLAUDE.md.template",
  "bootstrap/GEMINI.md.template",
  ...(CHECK_INSTALLED_COPY ? [
    ".agent-flow/bootstrap/AGENTS.md",
    ".agent-flow/bootstrap/CLAUDE.md",
    ".agent-flow/bootstrap/GEMINI.md",
  ] : []),
]) {
  assertFile(rel);
  assertContains(rel, 'agent-flow run "<task>"');
  assertContains(rel, "agent-flow status");
  assertContains(rel, "install은 프로젝트당 1회만");
  assertContains(rel, "next_command");
  assertContains(rel, "짧은 한글");
  assertContains(rel, "현재 사용 중인 CLI(활성 host)의 sub-agent 2개가 필수");
  assertContains(rel, "활성 host가 아닌 추가 provider는 optional");
  assertNotContains(rel, "예: Claude/Gemini");
  assertContains(rel, "reviewer-source: sub-agent");
  assertContains(rel, "sub-agent를 닫는다");
  assertContains(rel, "## Overall");
  assertContains(rel, "verdict: approve");
  assertContains(rel, "verdict: request-changes");
  assertContains(rel, "보호 브랜치 commit/push와 leader checkout/switch 금지는 Codex에서도 동일");
}

if (CHECK_INSTALLED_COPY) {
  assertContains(".agent-flow/rules/workflow-contract.md", "gates: all_passed");
  assertContains(".agent-flow/rules/workflow-contract.md", "short Korean");
  assertContains(".agent-flow/rules/workflow-contract.md", "two active-host sub-agents");
  assertContains(".agent-flow/rules/workflow-contract.md", "Gemini sub-agent in Gemini");
  assertContains(".agent-flow/rules/workflow-contract.md", "reviewer-source: sub-agent");
  assertContains(".agent-flow/rules/workflow-contract.md", "close that sub-agent session");
  assertContains(".agent-flow/rules/workflow-contract.md", "## Overall");
  assertContains(".agent-flow/rules/workflow-contract.md", "verdict: approve");
  assertContains(".agent-flow/rules/workflow-contract.md", "verdict: request-changes");
}

function resolveInstalledRoot(start) {
  const managedRoot = resolveManagedWorktreeRoot(start);
  if (managedRoot && fs.existsSync(path.join(managedRoot, ".agent-flow", "kit.json"))) {
    return managedRoot;
  }
  const gitCommonRoot = resolveGitCommonWorktreeRoot(start);
  if (gitCommonRoot && fs.existsSync(path.join(gitCommonRoot, ".agent-flow", "kit.json"))) {
    return gitCommonRoot;
  }
  let current = start;
  while (true) {
    if (fs.existsSync(path.join(current, ".agent-flow", "kit.json"))) {
      return current;
    }
    const parent = path.dirname(current);
    if (parent === current) {
      return null;
    }
    current = parent;
  }
}

function resolveManagedWorktreeRoot(start) {
  const parts = start.split(path.sep);
  const markers = new Set([".agent-flow", ".codex", ".Codex"]);
  for (let index = parts.length - 2; index >= 0; index -= 1) {
    if (parts[index + 1] !== "worktrees") continue;
    if (!markers.has(parts[index])) continue;
    const root = parts.slice(0, index).join(path.sep) || path.sep;
    // 홈의 전역 Codex worktree는 설치 루트가 아니라 git common root를 따라간다.
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
  const result = spawnSync("git", args, {
    cwd,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
    timeout: 30_000,
  });
  if (result.error || result.status !== 0) {
    return null;
  }
  const output = result.stdout.trim();
  return output || null;
}

function workflowExport(name) {
  const env = {
    ...process.env,
    PYTHONPATH: [path.join(SOURCE_ROOT, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
  };
  const result = spawnSync(preferredPython(), [
    "-m",
    "agent_flow.cli",
    "workflow",
    "export",
    "--workflow",
    name,
    "--format",
    "json",
  ], {
    cwd: SOURCE_ROOT,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    env,
    timeout: 30_000,
  });
  if (result.error || result.status !== 0) {
    failures.push(`workflow export failed: ${result.error?.message || result.stderr.trim() || result.status}`);
    return null;
  }
  try {
    const payload = JSON.parse(result.stdout);
    if (!Array.isArray(payload.phases)) {
      failures.push("workflow export phases must be an array");
      return null;
    }
    return payload;
  } catch (error) {
    failures.push(`workflow export invalid JSON: ${error.message}`);
    return null;
  }
}

function assertRouteParity(workflow) {
  const phaseIds = new Set(workflow.phases.map((phase) => phase.id));
  const cases = [
    ["plan-review verdict fail", "plan-review", "verdict: fail\n"],
    ["plan-review request", "plan-review", "verdict: request-changes\n"],
    ["plan-review bullet approve", "plan-review", "- verdict: approve\n"],
    ["pr-watch status passed", "pr-watch", "status: passed\n"],
    ["pr-watch status green", "pr-watch", "status: green\n"],
    ["pr-watch bullet status green", "pr-watch", "- status: green\n"],
    ["pr-watch note status green", "pr-watch", "note: status: green\n"],
    ["pr-watch indented status green", "pr-watch", "  status: green\n"],
    ["pr-watch status has_comments", "pr-watch", "status: has_comments\n"],
    ["pr-watch status has-comments", "pr-watch", "status: has-comments\n"],
    ["pr-watch status ci_failed", "pr-watch", "status: ci_failed\n"],
    ["gates passed without evidence", "gates", "{\"passed\": true}\n"],
    [
      "gates passed with evidence",
      "gates",
      "{\"passed\": true, \"results\": [{\"command\": \"npm test\", \"passed\": true, \"output\": \"ok\"}]}\n",
    ],
  ];
  for (const [label, phaseId, content] of cases) {
    if (!phaseIds.has(phaseId)) {
      continue;
    }
    const python = pythonPhaseOutcome(workflow, phaseId, content);
    const node = nodePhaseOutcome(workflow, phaseId, content);
    if (!python || !node) continue;
    if (python.route_key !== node.route_key) {
      failures.push(`python/node route key mismatch ${label}: python=${python.route_key} node=${node.route_key}`);
    }
    if (python.outcome !== node.outcome) {
      failures.push(`python/node route mismatch ${label}: python=${python.outcome} node=${node.outcome}`);
    }
  }
}

function assertCompletionMarkerPrefixParity(workflow) {
  const phase = workflow.phases.find((candidate) => candidate.id === "domain-grill");
  if (!phase) {
    return;
  }
  const content = [
    "## Completion Gate",
    "- [x] domain-grill: complete",
    "* shared_understanding: reached",
    "+ context_docs_checked: true",
    "- context_docs_updated: not_needed",
    "",
  ].join("\n");
  const pythonMissing = pythonMissingCompletionMarkers(content, phase.required_markers ?? []);
  const node = nodePhaseOutcome(workflow, phase.id, content);
  if (pythonMissing === null || !node) {
    return;
  }
  const pythonPasses = pythonMissing.length === 0;
  const nodePasses = node.outcome !== "blocked";
  if (pythonPasses !== nodePasses) {
    failures.push(`python/node completion marker prefix mismatch: python_missing=${pythonMissing.join(",")} node=${node.outcome}`);
  }
}

function assertFixLoopRoundParity(workflow) {
  const content = "{\"passed\": false}\n";
  const pythonAllowed = pythonPhaseOutcome(workflow, "gates", content, { fix_loop_rounds: 2 });
  const nodeAllowed = nodePhaseOutcome(workflow, "gates", content, { fix_loop_rounds: 2 });
  if (pythonAllowed && nodeAllowed) {
    if (pythonAllowed.outcome !== nodeAllowed.outcome || pythonAllowed.fix_loop_rounds !== nodeAllowed.fix_loop_rounds) {
      failures.push("python/node fix-loop round 3 mismatch");
    }
  }
  const pythonBlocked = pythonPhaseOutcome(workflow, "gates", content, { fix_loop_rounds: 3 });
  const nodeBlocked = nodePhaseOutcome(workflow, "gates", content, { fix_loop_rounds: 3 });
  if (pythonBlocked && nodeBlocked && (pythonBlocked.outcome !== "blocked" || nodeBlocked.outcome !== "blocked")) {
    failures.push(`python/node fix-loop round cap mismatch: python=${pythonBlocked.outcome} node=${nodeBlocked.outcome}`);
  }
  if (pythonBlocked && nodeBlocked && pythonBlocked.fix_loop_rounds !== nodeBlocked.fix_loop_rounds) {
    failures.push(`python/node blocked fix-loop round state mismatch: python=${pythonBlocked.fix_loop_rounds} node=${nodeBlocked.fix_loop_rounds}`);
  }
}

function assertBackwardFreshArtifactSafety(workflow) {
  for (const testCase of backwardFreshRouteCases(workflow)) {
    const python = pythonBackwardFreshArtifactOutcome(workflow, testCase);
    const node = nodeBackwardFreshArtifactOutcome(workflow, testCase);
    const label = `${workflow.id} ${testCase.source} ${testCase.key}->${testCase.target}`;
    if (python && python.remaining.length > 0) {
      failures.push(`python backward route left fresh artifacts (${label}): ${python.remaining.join(",")}`);
    }
    if (node && node.remaining.length > 0) {
      failures.push(`node backward route left fresh artifacts (${label}): ${node.remaining.join(",")}`);
    }
    if (python && node && python.remaining.join("|") !== node.remaining.join("|")) {
      failures.push(`python/node backward route cleanup mismatch (${label}): python=${python.remaining.join(",")} node=${node.remaining.join(",")}`);
    }
  }
}

function backwardFreshRouteCases(workflow) {
  const phaseIndex = new Map(workflow.phases.map((phase, index) => [phase.id, index]));
  const cases = [];
  for (const [index, phase] of workflow.phases.entries()) {
    if (!phase.routes || phase.multi_review) {
      continue;
    }
    for (const [key, target] of Object.entries(phase.routes)) {
      const targetIndex = phaseIndex.get(target);
      if (targetIndex === undefined || targetIndex > index) {
        continue;
      }
      const content = routeArtifactContent(phase, key);
      if (content !== null) {
        cases.push({ source: phase.id, target, key, content });
      }
    }
  }
  return cases;
}

function routeArtifactContent(phase, key) {
  if (phase.id === "plan-review" || phase.id === "architecture-review" || phase.id === "merge-approval") {
    return phaseArtifactWithMarkers(phase, `verdict: ${key}`);
  }
  if (phase.id === "pr-watch") {
    return phaseArtifactWithMarkers(phase, `status: ${key}`);
  }
  if (key === "default") {
    return phaseArtifactWithMarkers(phase, "");
  }
  return phaseArtifactWithMarkers(phase, `status: ${key}`);
}

function assertCleanInstallCopiesTemplates() {
  for (const installer of ["agent-flow-kit.mjs", "agent-flow-install.mjs"]) {
    assertInstallerCleanInstallCopiesTemplates(installer);
  }
}

function assertInstallerCleanInstallCopiesTemplates(installer) {
  const label = `bin/${installer}`;
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-install-parity-"));
  try {
    const seededHooksPath = path.join(tempRoot, ".Codex", "hooks.json");
    fs.mkdirSync(path.dirname(seededHooksPath), { recursive: true });
    fs.writeFileSync(
      seededHooksPath,
      `${JSON.stringify(
        {
          hooks: {
            PostToolUse: [
              {
                matcher: "CustomTool",
                hooks: [{ type: "command", command: "custom-post-hook" }],
              },
            ],
          },
        },
        null,
        2,
      )}\n`,
      "utf8",
    );
    const result = spawnSync(process.execPath, [path.join(SOURCE_ROOT, "bin", installer), "install", "--force-managed"], {
      cwd: tempRoot,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
      timeout: 30_000,
    });
    if (result.error || result.status !== 0) {
      failures.push(`${label} clean install parity failed: ${result.error?.message || result.stderr.trim() || result.status}`);
      return;
    }
    for (const rel of recursiveFiles("templates")) {
      const installedRel = `.agent-flow/${rel}`;
      const sourceText = fs.readFileSync(path.join(SOURCE_ROOT, rel), "utf8");
      const installedPath = path.join(tempRoot, installedRel);
      if (!fs.existsSync(installedPath)) {
        failures.push(`${label} clean install missing ${installedRel}`);
        continue;
      }
      if (fs.readFileSync(installedPath, "utf8") !== sourceText) {
        failures.push(`${label} clean install ${installedRel} differs from ${rel}`);
      }
    }
    const installedHooks = path.join(tempRoot, ".Codex", "hooks.json");
    if (!fs.existsSync(installedHooks)) {
      failures.push(`${label} clean install missing .Codex/hooks.json`);
    } else {
      const hooksText = fs.readFileSync(installedHooks, "utf8");
      if (!hooksText.includes("comment-checker.py")) {
        failures.push(`${label} clean install .Codex/hooks.json missing comment-checker hook`);
      }
      if (!hooksText.includes("custom-post-hook")) {
        failures.push(`${label} clean install .Codex/hooks.json did not preserve existing custom hook`);
      }
      if (!hooksText.includes(path.join(tempRoot, "scripts", "hooks", "comment-checker.py"))) {
        failures.push(`${label} clean install .Codex/hooks.json does not use project-local comment-checker`);
      }
      if (hooksText.includes(SOURCE_ROOT)) {
        failures.push(`${label} clean install .Codex/hooks.json leaks source root`);
      }
    }
    const installedChecker = path.join(tempRoot, "scripts", "hooks", "comment-checker.py");
    if (!fs.existsSync(installedChecker)) {
      failures.push(`${label} clean install missing scripts/hooks/comment-checker.py`);
    } else {
      try {
        fs.accessSync(installedChecker, fs.constants.X_OK);
      } catch {
        failures.push(`${label} clean install comment-checker.py is not executable`);
      }
    }
    const claudeSettingsPath = path.join(tempRoot, ".claude", "settings.json");
    if (!fs.existsSync(claudeSettingsPath)) {
      failures.push(`${label} clean install missing .claude/settings.json`);
    } else {
      const claudeSettingsText = fs.readFileSync(claudeSettingsPath, "utf8");
      if (!claudeSettingsText.includes("PostToolUse") || !claudeSettingsText.includes("comment-checker.py")) {
        failures.push(`${label} clean install .claude/settings.json missing comment-checker PostToolUse hook`);
      }
      if (!claudeSettingsText.includes(path.join(tempRoot, "scripts", "hooks", "comment-checker.py"))) {
        failures.push(`${label} clean install .claude/settings.json does not use project-local comment-checker`);
      }
      if (claudeSettingsText.includes(SOURCE_ROOT)) {
        failures.push(`${label} clean install .claude/settings.json leaks source root`);
      }
    }
    for (const skillName of ["comment-authoring-discipline", "comment-checker"]) {
      if (!fs.existsSync(path.join(tempRoot, ".agent-flow", "skills", skillName, "SKILL.md"))) {
        failures.push(`${label} clean install missing ${skillName} skill`);
      }
    }
    seedStaleForceManagedInstall(tempRoot);
    const forceResult = spawnSync(process.execPath, [path.join(SOURCE_ROOT, "bin", installer), "install", "--force-managed"], {
      cwd: tempRoot,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
      timeout: 30_000,
    });
    if (forceResult.error || forceResult.status !== 0) {
      failures.push(`${label} force install parity failed: ${forceResult.error?.message || forceResult.stderr.trim() || forceResult.status}`);
      return;
    }
    if (fs.existsSync(path.join(tempRoot, ".agent-flow", "templates", "_stale", "old.md"))) {
      failures.push(`${label} force install left stale .agent-flow/templates file`);
    }
    if (fs.existsSync(path.join(tempRoot, ".agent-flow", "workflows", "stale.yaml"))) {
      failures.push(`${label} force install left stale .agent-flow/workflows file`);
    }
    if (fs.existsSync(path.join(tempRoot, ".agent-flow", "skills", "stale-skill", "SKILL.md"))) {
      failures.push(`${label} force install left stale .agent-flow/skills file`);
    }
    for (const host of ["claude", "codex"]) {
      if (fs.existsSync(path.join(tempRoot, `.${host}`, "skills", "demo-stale", "SKILL.md"))) {
        failures.push(`${label} force install left previous-index stale ${host} skill link`);
      }
    }
    for (const name of [
      "architecture-reviewer",
      "ddd-clean-architecture",
      "full-feature-workflow",
      "plan-reviewer",
      "product-brief",
      "push-watch",
    ]) {
      if (!fs.existsSync(path.join(tempRoot, ".agent-flow", "skills", name, "SKILL.md"))) {
        failures.push(`${label} force install removed generated skill ${name}`);
      }
    }
    const indexPath = path.join(tempRoot, ".agent-flow", "skills", "index.json");
    if (!fs.existsSync(indexPath)) {
      failures.push(`${label} clean install missing .agent-flow/skills/index.json`);
    } else {
      assertHostSkillParity(tempRoot, JSON.parse(fs.readFileSync(indexPath, "utf8")), label);
    }
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true });
  }
}

function seedStaleForceManagedInstall(root) {
  const staleTemplate = path.join(root, ".agent-flow", "templates", "_stale", "old.md");
  fs.mkdirSync(path.dirname(staleTemplate), { recursive: true });
  fs.writeFileSync(staleTemplate, "stale\n", "utf8");
  const staleWorkflow = path.join(root, ".agent-flow", "workflows", "stale.yaml");
  fs.mkdirSync(path.dirname(staleWorkflow), { recursive: true });
  fs.writeFileSync(staleWorkflow, "id: stale\nphases: []\n", "utf8");
  const staleSkill = path.join(root, ".agent-flow", "skills", "stale-skill", "SKILL.md");
  fs.mkdirSync(path.dirname(staleSkill), { recursive: true });
  fs.writeFileSync(staleSkill, "---\nname: stale-skill\n---\n# Stale Skill\n", "utf8");
  for (const host of ["claude", "codex"]) {
    const skillDir = path.join(root, `.${host}`, "skills", "agent-flow");
    removePathOrSymlink(skillDir);
    fs.mkdirSync(skillDir, { recursive: true });
    fs.writeFileSync(path.join(skillDir, "SKILL.md"), `---\nname: agent-flow\n---\n# stale ${host}\n`, "utf8");
  }
  seedPreviousIndexStaleHostSkill(root);
}

function seedPreviousIndexStaleHostSkill(root) {
  const staleName = "demo-stale";
  const previousSkillText = "---\nname: demo-stale\n---\n# Previous Demo Stale\n";
  const currentHostText = "---\nname: demo-stale\n---\n# User Modified Demo Stale\n";
  for (const host of ["claude", "codex"]) {
    const skillDir = path.join(root, `.${host}`, "skills", staleName);
    removePathOrSymlink(skillDir);
    fs.mkdirSync(skillDir, { recursive: true });
    fs.writeFileSync(path.join(skillDir, "SKILL.md"), currentHostText, "utf8");
  }
  const indexPath = path.join(root, ".agent-flow", "skills", "index.json");
  const index = JSON.parse(fs.readFileSync(indexPath, "utf8"));
  const staleHash = createHash("sha256").update(previousSkillText).digest("hex");
  index.skills = [
    ...(index.skills ?? []),
    {
      name: staleName,
      source: "project",
      path: "skills/demo-stale/SKILL.md",
      hosts: ["claude", "codex"],
      priority: 50,
      hash: staleHash,
      warnings: [],
    },
  ];
  index.links = [
    ...(index.links ?? []),
    { name: staleName, host: "claude", path: ".claude/skills/demo-stale", status: "copied" },
    { name: staleName, host: "codex", path: ".codex/skills/demo-stale", status: "copied" },
  ];
  fs.writeFileSync(indexPath, `${JSON.stringify(index, null, 2)}\n`, "utf8");
}

function removePathOrSymlink(target) {
  try {
    const stat = fs.lstatSync(target);
    if (stat.isSymbolicLink()) {
      fs.unlinkSync(target);
    } else {
      fs.rmSync(target, { recursive: true, force: true });
    }
  } catch (error) {
    if (!error || error.code !== "ENOENT") {
      throw error;
    }
  }
}

function pythonBackwardFreshArtifactOutcome(workflow, testCase) {
  const code = String.raw`
import json
import sys
import tempfile
from pathlib import Path

from agent_flow.runner import Phase, Runner

payload = json.loads(sys.stdin.read())
workflow = payload["workflow"]
test_case = payload["test_case"]
with tempfile.TemporaryDirectory() as temp_dir:
    run_dir = Path(temp_dir)
    phases = []
    for item in workflow["phases"]:
        phases.append(Phase(
            id=item["id"],
            description=item.get("description", ""),
            artifact=item.get("artifact", f"{item['id']}.md"),
            multi_review=bool(item.get("multi_review", False)),
            routes=item.get("routes"),
        ))
    target_index = next(i for i, phase in enumerate(phases) if phase.id == test_case["target"])
    current_index = next(i for i, phase in enumerate(phases) if phase.id == test_case["source"])
    for phase in phases[target_index:current_index + 1]:
        artifact = run_dir / phase.artifact
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(test_case["content"] if phase.id == test_case["source"] else "stale\n", encoding="utf-8")
    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.phases = phases
    runner._next_index(current_index, phases[current_index])
    remaining = []
    for phase in phases[target_index:current_index + 1]:
        if (run_dir / phase.artifact).exists():
            remaining.append(phase.id)
    print(json.dumps({"remaining": remaining}, sort_keys=True))
`;
  const result = spawnSync(preferredPython(), ["-c", code], {
    cwd: SOURCE_ROOT,
    input: JSON.stringify({ workflow, test_case: testCase }),
    encoding: "utf8",
    env: {
      ...process.env,
      PYTHONPATH: [path.join(SOURCE_ROOT, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
    },
    stdio: ["pipe", "pipe", "pipe"],
    timeout: 30_000,
  });
  if (result.error || result.status !== 0) {
    failures.push(`python backward fresh artifact check failed: ${result.error?.message || result.stderr.trim() || result.status}`);
    return null;
  }
  return JSON.parse(result.stdout);
}

function nodeBackwardFreshArtifactOutcome(workflow, testCase) {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-backward-parity-"));
  try {
    const install = spawnSync(process.execPath, [path.join(SOURCE_ROOT, "bin/agent-flow-kit.mjs"), "install", "--force-managed"], {
      cwd: tempRoot,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
      timeout: 30_000,
    });
    if (install.error || install.status !== 0) {
      failures.push(`node backward parity install failed: ${install.error?.message || install.stderr.trim() || install.status}`);
      return null;
    }
    const start = spawnSync(process.execPath, [
      path.join(SOURCE_ROOT, "bin/agent-flow-kit.mjs"),
      "run",
      "start",
      "--workflow",
      workflow.id,
      "--task",
      "backward-parity",
      "--run-id",
      "r1",
    ], {
      cwd: tempRoot,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
      timeout: 30_000,
    });
    if (start.error || start.status !== 0) {
      failures.push(`node backward parity start failed: ${start.error?.message || start.stderr.trim() || start.status}`);
      return null;
    }
    const targetIndex = workflow.phases.findIndex((phase) => phase.id === testCase.target);
    const currentIndex = workflow.phases.findIndex((phase) => phase.id === testCase.source);
    const runDir = path.join(tempRoot, ".agent-flow", "runs", workflow.id, "r1");
    const manifestPath = path.join(runDir, "manifest.json");
    const statePath = path.join(tempRoot, ".agent-flow", "state", "current-run.json");
    const state = {
      ...JSON.parse(fs.readFileSync(manifestPath, "utf8")),
      phase_index: currentIndex,
      phase: testCase.source,
      status: "running",
      phase_entered_at: "2000-01-01T00:00:00.000Z",
    };
    fs.writeFileSync(manifestPath, `${JSON.stringify(state, null, 2)}\n`, "utf8");
    fs.writeFileSync(statePath, `${JSON.stringify(state, null, 2)}\n`, "utf8");
    for (const stalePhase of workflow.phases.slice(targetIndex, currentIndex + 1)) {
      const artifact = path.join(runDir, stalePhase.artifact);
      fs.mkdirSync(path.dirname(artifact), { recursive: true });
      fs.writeFileSync(
        artifact,
        stalePhase.id === testCase.source ? testCase.content : "stale\n",
        "utf8",
      );
    }
    const advance = spawnSync(process.execPath, [path.join(SOURCE_ROOT, "bin/agent-flow-kit.mjs"), "run", "advance"], {
      cwd: tempRoot,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
      timeout: 30_000,
    });
    if (advance.error || advance.status !== 0) {
      return { remaining: [`route-blocked:${advance.stderr.trim() || advance.status}`] };
    }
    const remaining = [];
    for (const stalePhase of workflow.phases.slice(targetIndex, currentIndex + 1)) {
      if (fs.existsSync(path.join(runDir, stalePhase.artifact))) {
        remaining.push(stalePhase.id);
      }
    }
    return { remaining };
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true });
  }
}

function phaseArtifactWithMarkers(phase, routeLine) {
  const markers = phase.required_markers ?? [];
  const headings = markers.filter((marker) => marker.trim().startsWith("#"));
  const lines = markers
    .filter((marker) => !marker.trim().startsWith("#"))
    .map(renderCompletionMarker);
  return [
    routeLine,
    "",
    ...headings,
    "",
    "## Completion Gate",
    ...lines,
    "",
  ].join("\n");
}

function renderCompletionMarker(marker) {
  const trimmed = marker.trim();
  if (trimmed.endsWith(":")) {
    return `${trimmed} n/a`;
  }
  const separator = trimmed.indexOf(":");
  if (separator !== -1 && trimmed.slice(separator + 1).includes("|")) {
    const key = trimmed.slice(0, separator).trim();
    const value = trimmed
      .slice(separator + 1)
      .split("|")
      .map((item) => item.trim())
      .filter(Boolean)[0] ?? "n/a";
    return `${key}: ${value}`;
  }
  return trimmed;
}

function assertHostSkillParity(root, index, label = "clean install") {
  const skills = new Map((index.skills ?? []).map((skill) => [skill.name, skill]));
  const links = index.links ?? [];
  for (const [name, skill] of skills) {
    const hosts = new Set(skill.hosts ?? []);
    if (!hosts.has("claude") || !hosts.has("codex")) {
      continue;
    }
    const claudeSkill = path.join(root, ".claude", "skills", name, "SKILL.md");
    const codexSkill = path.join(root, ".codex", "skills", name, "SKILL.md");
    const sourceSkill = path.join(root, skill.path);
    if (!fs.existsSync(claudeSkill)) {
      failures.push(`${label} missing Claude skill link for ${name}`);
      continue;
    }
    if (!fs.existsSync(codexSkill)) {
      failures.push(`${label} missing Codex skill link for ${name}`);
      continue;
    }
    if (!fs.existsSync(sourceSkill)) {
      failures.push(`${label} missing project skill source for ${name}`);
      continue;
    }
    if (fs.readFileSync(claudeSkill, "utf8") !== fs.readFileSync(codexSkill, "utf8")) {
      failures.push(`${label} Claude/Codex skill content differs for ${name}`);
    }
    if (fs.readFileSync(claudeSkill, "utf8") !== fs.readFileSync(sourceSkill, "utf8")) {
      failures.push(`${label} host/project skill content differs for ${name}`);
    }
    const claudeLink = links.find((link) => link.name === name && link.host === "claude");
    const codexLink = links.find((link) => link.name === name && link.host === "codex");
    if (!claudeLink || !codexLink) {
      failures.push(`${label} missing Claude/Codex index link pair for ${name}`);
      continue;
    }
    if (claudeLink.status !== codexLink.status) {
      failures.push(`${label} Claude/Codex link status differs for ${name}: claude=${claudeLink.status} codex=${codexLink.status}`);
    }
  }
}

var nodeRouteKeyEvaluator = null;

function nodeRouteKeyFromKit(phase, content) {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-route-key-"));
  try {
    const artifact = path.join(tempRoot, "artifact.txt");
    fs.writeFileSync(artifact, content, "utf8");
    if (!nodeRouteKeyEvaluator) {
      nodeRouteKeyEvaluator = buildNodeRouteKeyEvaluator();
    }
    return nodeRouteKeyEvaluator(phase, artifact);
  } catch (error) {
    return `blocked:${error.message}`;
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true });
  }
}

function buildNodeRouteKeyEvaluator() {
  const source = read("bin/agent-flow-kit.mjs");
  const start = source.indexOf("function assertCompletionMarkers");
  const end = source.indexOf("function upsertBootstrapBlock");
  if (start === -1 || end === -1 || end <= start) {
    failures.push("node route key evaluator source extraction failed");
    return () => "blocked:source-extraction";
  }
  const routeKeySource = source.slice(start, end);
  return new Function(
    "fs",
    "phase",
    "artifact",
    `${routeKeySource}\nreturn nodeRouteKey(phase, artifact);`,
  ).bind(null, fs);
}

function pythonPhaseOutcome(workflow, phaseId, content, meta = {}) {
  const code = String.raw`
import json
import sys
import tempfile
from pathlib import Path

from agent_flow.artifact import read_meta, write_meta
from agent_flow.core.markers import has_failure_markers
from agent_flow.runner import (
    Phase,
    Runner,
    _gates_route_key,
    _multi_review_route_key,
    _route_key,
)

payload = json.loads(sys.stdin.read())
workflow = payload["workflow"]
phase_id = payload["phase_id"]
content = payload["content"]

with tempfile.TemporaryDirectory() as temp_dir:
    run_dir = Path(temp_dir)
    write_meta(run_dir, payload.get("meta", {}))
    phases = []
    for item in workflow["phases"]:
        phases.append(Phase(
            id=item["id"],
            description=item.get("description", ""),
            artifact=item.get("artifact", f"{item['id']}.md"),
            multi_review=bool(item.get("multi_review", False)),
            routes=item.get("routes"),
        ))
    index = next(i for i, phase in enumerate(phases) if phase.id == phase_id)
    artifact = run_dir / phases[index].artifact
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(content, encoding="utf-8")
    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.phases = phases
    if phases[index].multi_review:
        route_key = _multi_review_route_key(content, phases[index].id)
    elif phases[index].id == "gates":
        route_key = _gates_route_key(content)
    else:
        route_key = _route_key(content)
    if route_key == "approve" and phases[index].routes and phases[index].routes.get("request-changes") and has_failure_markers(content):
        route_key = "request-changes"
    try:
        next_index, blocked = runner._next_index(index, phases[index])
        outcome = "blocked" if blocked else (phases[next_index].id if next_index < len(phases) else "complete")
    except Exception:
        outcome = "blocked"
    print("OUTCOME:" + json.dumps({"outcome": outcome, "route_key": route_key, "fix_loop_rounds": read_meta(run_dir).get("fix_loop_rounds")}, sort_keys=True))
`;
  const result = spawnSync(preferredPython(), ["-c", code], {
    cwd: SOURCE_ROOT,
    input: JSON.stringify({ workflow, phase_id: phaseId, content, meta }),
    encoding: "utf8",
    env: {
      ...process.env,
      PYTHONPATH: [path.join(SOURCE_ROOT, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
    },
    stdio: ["pipe", "pipe", "pipe"],
    timeout: 30_000,
  });
  if (result.error || result.status !== 0) {
    failures.push(`python route parity failed: ${result.error?.message || result.stderr.trim() || result.status}`);
    return null;
  }
  const match = result.stdout.match(/OUTCOME:(.+)$/m);
  if (!match) {
    failures.push("python route parity missing outcome");
    return null;
  }
  return JSON.parse(match[1]);
}

function pythonMissingCompletionMarkers(content, markers) {
  const code = String.raw`
import json
import sys

from agent_flow.core.markers import missing_markers

payload = json.loads(sys.stdin.read())
print(json.dumps(missing_markers(payload["content"], tuple(payload["markers"]))))
`;
  const result = spawnSync(preferredPython(), ["-c", code], {
    cwd: SOURCE_ROOT,
    input: JSON.stringify({ content, markers }),
    encoding: "utf8",
    env: {
      ...process.env,
      PYTHONPATH: [path.join(SOURCE_ROOT, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
    },
    stdio: ["pipe", "pipe", "pipe"],
    timeout: 30_000,
  });
  if (result.error || result.status !== 0) {
    failures.push(`python marker parity failed: ${result.error?.message || result.stderr.trim() || result.status}`);
    return null;
  }
  return JSON.parse(result.stdout);
}

function nodePhaseOutcome(workflow, phaseId, content, stateOverrides = {}) {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-parity-"));
  try {
    const install = spawnSync(process.execPath, [path.join(SOURCE_ROOT, "bin/agent-flow-kit.mjs"), "install", "--force-managed"], {
      cwd: tempRoot,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
      timeout: 30_000,
    });
    if (install.error || install.status !== 0) {
      failures.push(`node route parity install failed: ${install.error?.message || install.stderr.trim() || install.status}`);
      return null;
    }
    const start = spawnSync(process.execPath, [
      path.join(SOURCE_ROOT, "bin/agent-flow-kit.mjs"),
      "run",
      "start",
      "--workflow",
      workflow.id,
      "--task",
      "parity",
      "--run-id",
      "r1",
    ], {
      cwd: tempRoot,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
      timeout: 30_000,
    });
    if (start.error || start.status !== 0) {
      failures.push(`node route parity start failed: ${start.error?.message || start.stderr.trim() || start.status}`);
      return null;
    }
    const phaseIndex = workflow.phases.findIndex((phase) => phase.id === phaseId);
    const phase = workflow.phases[phaseIndex];
    const runDir = path.join(tempRoot, ".agent-flow", "runs", workflow.id, "r1");
    const manifestPath = path.join(runDir, "manifest.json");
    const statePath = path.join(tempRoot, ".agent-flow", "state", "current-run.json");
    const state = {
      ...JSON.parse(fs.readFileSync(manifestPath, "utf8")),
      ...stateOverrides,
      phase_index: phaseIndex,
      phase: phaseId,
      status: "running",
      phase_entered_at: "2000-01-01T00:00:00.000Z",
    };
    fs.writeFileSync(manifestPath, `${JSON.stringify(state, null, 2)}\n`, "utf8");
    fs.writeFileSync(statePath, `${JSON.stringify(state, null, 2)}\n`, "utf8");
    const artifact = path.join(runDir, phase.artifact);
    fs.mkdirSync(path.dirname(artifact), { recursive: true });
    fs.writeFileSync(artifact, content, "utf8");
    const routeKey = nodeRouteKeyFromKit(phase, content);
    const advance = spawnSync(process.execPath, [path.join(SOURCE_ROOT, "bin/agent-flow-kit.mjs"), "run", "advance"], {
      cwd: tempRoot,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
      timeout: 30_000,
    });
    const nextState = JSON.parse(fs.readFileSync(statePath, "utf8"));
    return {
      outcome: advance.status === 0 ? nextState.phase : "blocked",
      route_key: routeKey,
      fix_loop_rounds: nextState.fix_loop_rounds,
    };
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true });
  }
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
    const result = spawnSync(candidate, ["--version"], {
      stdio: "ignore",
      timeout: 30_000,
    });
    if (!result.error && result.status === 0 && pythonSupportsWorkflowExport(candidate)) {
      return candidate;
    }
  }
  return "python3";
}

function pythonSupportsWorkflowExport(candidate) {
  const result = spawnSync(candidate, ["-c", "import yaml"], {
    stdio: "ignore",
    timeout: 5_000,
  });
  return !result.error && result.status === 0;
}

if (failures.length > 0) {
  console.error(`agent-flow-parity: FAIL (${failures.length})`);
  for (const failure of failures) {
    console.error(`- ${failure}`);
  }
  process.exit(1);
}

console.log("agent-flow-parity: OK");
