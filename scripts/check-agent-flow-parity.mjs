#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";

const ROOT = process.cwd();
const failures = [];
const missingFiles = new Set();

function read(rel) {
  return fs.readFileSync(path.join(ROOT, rel), "utf8");
}

function readIfExists(rel) {
  const abs = path.join(ROOT, rel);
  if (!fs.existsSync(abs)) {
    recordMissingFile(rel);
    return null;
  }
  return fs.readFileSync(abs, "utf8");
}

function assertFile(rel) {
  if (!fs.existsSync(path.join(ROOT, rel))) {
    recordMissingFile(rel);
  }
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
  ".agent-flow/workflows/full-feature.yaml",
];

// phase 제거가 source/generated copy 중 한 곳에만 반영되는 drift를 막는다.
for (const rel of fullFeatureWorkflowCopies) {
  assertFile(rel);
  assertContains(rel, "id: domain-grill");
  assertContains(rel, "context_docs_updated: true|not_needed");
  assertContains(rel, "Default reviewer is an active-host sub-agent");
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
assertContains("workflows/default.yaml", "active-host reviewer sub-agent");
assertContains("workflows/default.yaml", "reviewer-source: sub-agent");
assertContains("workflows/default.yaml", "close that sub-agent session");
assertContains("workflows/default.yaml", "## Overall");
assertContains("workflows/default.yaml", "verdict: approve");
assertContains("workflows/default.yaml", "verdict: request-changes");
assertContains(".agent-flow/prompts/multi-review.md", "Default reviewer is an active-host sub-agent");
assertContains(".agent-flow/prompts/multi-review.md", "reviewer-source: sub-agent");
assertContains(".agent-flow/prompts/multi-review.md", "close that sub-agent session");
assertContains(".agent-flow/prompts/multi-review.md", "## Overall");
assertContains(".agent-flow/prompts/multi-review.md", "verdict: approve");
assertContains(".agent-flow/prompts/multi-review.md", "verdict: request-changes");

// skill source와 설치본이 달라지면 다른 프로젝트로 전파될 때 기준이 갈린다.
assertSame("skills/grill-with-docs/SKILL.md", ".agent-flow/skills/grill-with-docs/SKILL.md");
assertSame("skills/agent-flow/SKILL.md", ".agent-flow/skills/agent-flow/SKILL.md");
assertSame("skills/code-generation-discipline/SKILL.md", ".agent-flow/skills/code-generation-discipline/SKILL.md");

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

for (const entry of fs.readdirSync(path.join(ROOT, "profiles")).sort()) {
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
  ".agent-flow/bootstrap/AGENTS.md",
  ".agent-flow/bootstrap/CLAUDE.md",
  ".agent-flow/bootstrap/GEMINI.md",
]) {
  assertFile(rel);
  assertContains(rel, 'agent-flow run "<task>"');
  assertContains(rel, "agent-flow status");
  assertContains(rel, "install은 프로젝트당 1회만");
  assertContains(rel, "next_command");
  assertContains(rel, "짧은 한글");
  assertContains(rel, "현재 사용 중인 CLI의 sub-agent 1개가 필수");
  assertContains(rel, "reviewer-source: sub-agent");
  assertContains(rel, "sub-agent를 닫는다");
  assertContains(rel, "## Overall");
  assertContains(rel, "verdict: approve");
  assertContains(rel, "verdict: request-changes");
}

assertContains(".agent-flow/rules/workflow-contract.md", "gates: all_passed");
assertContains(".agent-flow/rules/workflow-contract.md", "short Korean");
assertContains(".agent-flow/rules/workflow-contract.md", "one active-host sub-agent");
assertContains(".agent-flow/rules/workflow-contract.md", "reviewer-source: sub-agent");
assertContains(".agent-flow/rules/workflow-contract.md", "close that sub-agent session");
assertContains(".agent-flow/rules/workflow-contract.md", "## Overall");
assertContains(".agent-flow/rules/workflow-contract.md", "verdict: approve");
assertContains(".agent-flow/rules/workflow-contract.md", "verdict: request-changes");

if (failures.length > 0) {
  console.error(`agent-flow-parity: FAIL (${failures.length})`);
  for (const failure of failures) {
    console.error(`- ${failure}`);
  }
  process.exit(1);
}

console.log("agent-flow-parity: OK");
