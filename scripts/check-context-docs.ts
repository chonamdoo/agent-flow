#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

type ContextTreeRecord = {
  id?: unknown;
  parent?: unknown;
  path?: unknown;
  summary?: unknown;
};

type ManagedWorktreeContext = {
  leaderRoot: string;
  worktreeRoot: string;
};

const SCRIPT_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const INSTALLED_ROOT = path.basename(SCRIPT_ROOT) === ".agent-flow" ? path.dirname(SCRIPT_ROOT) : SCRIPT_ROOT;
const ROOT = resolveContextRoot(INSTALLED_ROOT, process.cwd());
const CONTEXT = path.join(ROOT, "CONTEXT.md");
const CONTEXT_TREE = path.join(ROOT, ".Codex", "context", "tree.jsonl");
const MAX_HOT_DOC_LINES = 200;
const ABSOLUTE_PATH_RE =
  /(?<![\w.-])(?:\/Users\/|\/home\/|\/private\/var\/|\/workspace\/|\/tmp\/|\/var\/|\/opt\/|\/mnt\/|[A-Za-z]:[\\/])/;
const CONFLICT_RE = /^(<<<<<<<|=======|>>>>>>>)$/m;
const FUTURE_TERMS = ["Worker", "Task", "Team State", "Mailbox", "Heartbeat"];

const errors: string[] = [];
checkContext(errors);
checkContextTree(errors);
checkRepoDocs(errors);
checkAgentFlowArtifacts(errors);

if (errors.length > 0) {
  for (const error of errors) {
    console.log(`context-docs: FAIL: ${error}`);
  }
  process.exit(1);
}

console.log("context-docs: OK");

function resolveContextRoot(installedRoot: string, cwd: string): string {
  const worktree = managedWorktreeContext(cwd);
  if (worktree !== undefined && samePath(worktree.leaderRoot, installedRoot)) {
    return worktree.worktreeRoot;
  }
  return installedRoot;
}

function managedWorktreeContext(cwd: string): ManagedWorktreeContext | undefined {
  const resolved = path.resolve(cwd);
  const parts = resolved.split(path.sep);
  for (let index = parts.length - 3; index > 0; index -= 1) {
    if (parts[index] !== ".agent-flow" || parts[index + 1] !== "worktrees") {
      continue;
    }
    return {
      leaderRoot: parts.slice(0, index).join(path.sep) || path.sep,
      worktreeRoot: parts.slice(0, index + 3).join(path.sep) || path.sep,
    };
  }
  return undefined;
}

function samePath(left: string, right: string): boolean {
  try {
    return fs.realpathSync.native(left) === fs.realpathSync.native(right);
  } catch {
    return path.resolve(left) === path.resolve(right);
  }
}

function checkContext(result: string[]): void {
  if (!fs.existsSync(CONTEXT)) {
    result.push("CONTEXT.md missing");
    return;
  }
  const text = readText(CONTEXT);
  checkText("CONTEXT.md", text, result);
  const current = sectionUntilNextHeading(text, "Current Vocabulary");
  if (current === undefined) {
    result.push("CONTEXT.md must include Current Vocabulary before Future Vocabulary");
  }
  for (const term of FUTURE_TERMS) {
    if (new RegExp(`\\b${escapeRegExp(term)}\\b`, "i").test(current ?? "")) {
      result.push(`future term used in current vocabulary: ${term}`);
    }
  }
  if (!text.includes("Future Vocabulary")) {
    result.push("CONTEXT.md must separate current and future vocabulary");
  }
}

function checkContextTree(result: string[]): void {
  if (!fs.existsSync(CONTEXT_TREE)) {
    result.push("required context tree missing: .Codex/context/tree.jsonl");
    return;
  }
  const records = new Map<string, ContextTreeRecord>();
  const indexedFiles = new Set<string>();
  const lines = readText(CONTEXT_TREE).split(/\r?\n/);
  for (const [index, line] of lines.entries()) {
    const lineNumber = index + 1;
    if (!line.trim()) {
      continue;
    }
    let record: ContextTreeRecord;
    try {
      record = JSON.parse(line) as ContextTreeRecord;
    } catch (error) {
      result.push(`.Codex/context/tree.jsonl:${lineNumber}: invalid JSONL: ${(error as Error).message}`);
      continue;
    }
    const nodeId = record.id;
    const relPath = record.path;
    const summary = record.summary;
    const parent = record.parent;
    if (typeof nodeId !== "string" || nodeId.length === 0) {
      result.push(`.Codex/context/tree.jsonl:${lineNumber}: id is required`);
      continue;
    }
    if (records.has(nodeId)) {
      result.push(`.Codex/context/tree.jsonl:${lineNumber}: duplicate id: ${nodeId}`);
    }
    records.set(nodeId, record);
    if (parent !== null && parent !== undefined && typeof parent !== "string") {
      result.push(`.Codex/context/tree.jsonl:${lineNumber}: parent must be string or null`);
    }
    if (typeof summary !== "string" || summary.trim().length === 0) {
      result.push(`.Codex/context/tree.jsonl:${lineNumber}: summary is required`);
    }
    if (typeof relPath !== "string" || relPath.length === 0) {
      result.push(`.Codex/context/tree.jsonl:${lineNumber}: path is required`);
      continue;
    }
    if (path.isAbsolute(relPath) || ABSOLUTE_PATH_RE.test(relPath)) {
      result.push(`.Codex/context/tree.jsonl:${lineNumber}: path must be repo-relative: ${relPath}`);
      continue;
    }
    const nodePath = path.join(ROOT, relPath);
    if (!fs.existsSync(nodePath)) {
      result.push(`.Codex/context/tree.jsonl:${lineNumber}: node path missing: ${relPath}`);
      continue;
    }
    if (fs.statSync(nodePath).isFile()) {
      indexedFiles.add(path.resolve(nodePath));
      const text = readText(nodePath);
      const lineCount = countLines(text);
      if (lineCount >= MAX_HOT_DOC_LINES) {
        result.push(`${relPath} has ${lineCount} lines; split into leaf docs under ${MAX_HOT_DOC_LINES} lines`);
      }
      checkText(relPath, text, result);
    }
  }
  for (const [nodeId, record] of records.entries()) {
    if (typeof record.parent === "string" && !records.has(record.parent)) {
      result.push(`.Codex/context/tree.jsonl: parent missing for ${nodeId}: ${record.parent}`);
    }
  }
  for (const file of listMarkdownFiles(path.join(ROOT, ".Codex", "rules", "context"))) {
    if (!indexedFiles.has(path.resolve(file))) {
      result.push(`context leaf doc missing from tree.jsonl: ${relative(file)}`);
    }
  }
}

function checkRepoDocs(result: string[]): void {
  for (const rel of requiredContextFiles()) {
    if (!fs.existsSync(path.join(ROOT, rel))) {
      result.push(`required context file missing: ${rel}`);
    }
  }
  const candidates = [
    path.join(ROOT, "AGENTS.md"),
    path.join(ROOT, ".Codex", "rules"),
    path.join(ROOT, "docs"),
    path.join(ROOT, "workflows"),
  ];
  for (const candidate of candidates) {
    if (!fs.existsSync(candidate)) {
      continue;
    }
    const stat = fs.statSync(candidate);
    if (stat.isFile()) {
      checkText(relative(candidate), readText(candidate), result);
    } else if (stat.isDirectory()) {
      for (const file of listMarkdownFiles(candidate)) {
        checkText(relative(file), readText(file), result);
      }
    }
  }
}

function checkAgentFlowArtifacts(result: string[]): void {
  const agentFlow = path.join(ROOT, ".agent-flow");
  if (!fs.existsSync(agentFlow)) {
    return;
  }
  const candidates: string[] = [];
  for (const relDir of ["runs", "handoffs", "memory"]) {
    const artifactDir = path.join(agentFlow, relDir);
    if (!fs.existsSync(artifactDir)) {
      continue;
    }
    for (const file of listFiles(artifactDir)) {
      if ([".md", ".json", ".jsonl"].includes(path.extname(file))) {
        candidates.push(file);
      }
    }
  }
  // tracked-only가 아니라 not-ignored를 검사한다. ignore된 artifact는 커밋될 수
  // 없어 "절대경로가 커밋에 샌다"가 성립하지 않지만, `.agent-flow/`를 무시하지
  // 않는 소비자 저장소에서는 아직 `git add` 전인 artifact도 계속 잡아야 한다.
  const ignored = gitIgnoredPaths(candidates);
  for (const file of candidates) {
    if (ignored.has(file)) {
      continue;
    }
    if (ABSOLUTE_PATH_RE.test(readText(file))) {
      result.push(`absolute local path in Agent Flow artifact: ${relative(file)}`);
    }
  }
}

function gitIgnoredPaths(files: string[]): Set<string> {
  if (files.length === 0) {
    return new Set();
  }
  const probe = spawnSync("git", ["check-ignore", "-z", "--stdin"], {
    cwd: ROOT,
    input: files.map((file) => `${file}\0`).join(""),
    encoding: "utf8",
    timeout: 10_000,
  });
  // exit 1 = ignore된 경로 없음. 그 밖의 실패는 git이 대답하지 못한 것이므로
  // 아무것도 빼지 않고 전부 검사한다(fail-closed).
  if (probe.error || (probe.status !== 0 && probe.status !== 1)) {
    return new Set();
  }
  return new Set(
    probe.stdout
      .split("\0")
      .filter((line) => line.length > 0)
      .map((line) => path.resolve(ROOT, line)),
  );
}

function checkText(label: string, text: string, result: string[]): void {
  if (CONFLICT_RE.test(text)) {
    result.push(`conflict marker in ${label}`);
  }
  if (ABSOLUTE_PATH_RE.test(text)) {
    result.push(`absolute local path in ${label}`);
  }
}

function countLines(text: string): number {
  if (text.length === 0) {
    return 0;
  }
  return text.replace(/\r?\n$/, "").split(/\r?\n/).length;
}

function requiredContextFiles(): string[] {
  return [
    ".Codex/rules/context/domain-glossary-full.md",
    ".Codex/rules/context/research-context.md",
    ".Codex/rules/context/paper-runtime-context.md",
    ".Codex/rules/context/agent-flow-context-map.md",
    ".Codex/rules/context/context-maintenance.md",
    ".Codex/context/tree.jsonl",
  ];
}

function sectionUntilNextHeading(text: string, start: string): string | undefined {
  const match = text.match(new RegExp(`^## ${escapeRegExp(start)}\\s*$([\\s\\S]*?)(?=^## |\\Z)`, "m"));
  return match?.[1];
}

function listMarkdownFiles(dir: string): string[] {
  return listFiles(dir).filter((file) => path.extname(file) === ".md");
}

function listFiles(dir: string): string[] {
  if (!fs.existsSync(dir)) {
    return [];
  }
  const files: string[] = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...listFiles(fullPath));
    } else if (entry.isFile()) {
      files.push(fullPath);
    }
  }
  return files;
}

function readText(file: string): string {
  return fs.readFileSync(file, "utf8");
}

function relative(file: string): string {
  return path.relative(ROOT, file);
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
