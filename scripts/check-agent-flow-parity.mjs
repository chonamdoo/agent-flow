#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
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
  if (exportedPhases["gates"]?.routes?.green !== "multi-review") {
    failures.push("workflow export gates green route mismatch");
  }
  if (exportedPhases["red"]?.artifact !== "artifacts/red.log") {
    failures.push("workflow export red artifact mismatch");
  }
}

// phase 제거가 source/generated copy 중 한 곳에만 반영되는 drift를 막는다.
for (const rel of fullFeatureWorkflowCopies) {
  assertFile(rel);
  assertContains(rel, "id: domain-grill");
  assertContains(rel, "context_docs_updated: true|not_needed");
  assertContains(rel, "skills_checked: true");
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

assertSame("workflows/default.yaml", "src/agent_flow/workflows/default.yaml");
assertSame("workflows/full-feature.yaml", "src/agent_flow/workflows/full-feature.yaml");
assertContains("workflows/default.yaml", "active-host reviewer sub-agents");
assertContains("workflows/default.yaml", "Gemini sub-agent in Gemini");
assertContains("workflows/default.yaml", "reviewer-source: sub-agent");
assertContains("workflows/default.yaml", "close that sub-agent session");
assertContains("workflows/default.yaml", "## Overall");
assertContains("workflows/default.yaml", "verdict: approve");
assertContains("workflows/default.yaml", "verdict: request-changes");
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

for (const entry of fs.readdirSync(path.join(SOURCE_ROOT, "profiles")).sort()) {
  if (!entry.endsWith(".yaml") || entry === "_schema.yaml") continue;
  const source = `profiles/${entry}`;
  const packaged = `src/agent_flow/profiles/${entry}`;
  const sourceText = readIfExists(source);
  const packagedText = readIfExists(packaged);
  if (sourceText === null || packagedText === null) continue;
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
    if (!result.error && result.status === 0) {
      return candidate;
    }
  }
  return "python3";
}

if (failures.length > 0) {
  console.error(`agent-flow-parity: FAIL (${failures.length})`);
  for (const failure of failures) {
    console.error(`- ${failure}`);
  }
  process.exit(1);
}

console.log("agent-flow-parity: OK");
