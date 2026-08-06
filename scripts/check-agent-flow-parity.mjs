#!/usr/bin/env node

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";

import {
  activeInstallProfileIds,
  installedProfileFileNames,
  PROJECT_LAUNCHER_RELATIVE,
  resolveLinkedWorktreeLeader,
  resolveManagedWorktreeContext,
} from "../lib/installer-shared.mjs";

// Python `LEAKY_GIT_ENV_VARS`(`src/agent_flow/core/worktree_isolation.py`) 및 두
// installer와 같은 목록이다. ambient discovery 변수가 남아 있으면 이 검사가 검사
// 대상이 아닌 다른 checkout을 보고 통과/실패를 낸다. INSTALL_ROOT 계산이 모듈
// 최상단에서 git을 부르므로 그보다 위에 있어야 한다(아래면 TDZ로 죽는다).
const LEAKY_GIT_ENV_VARS = [
  "GIT_DIR",
  "GIT_WORK_TREE",
  "GIT_COMMON_DIR",
  "GIT_INDEX_FILE",
  "GIT_OBJECT_DIRECTORY",
  "GIT_ALTERNATE_OBJECT_DIRECTORIES",
  "GIT_NAMESPACE",
  "GIT_PREFIX",
  "GIT_CEILING_DIRECTORIES",
];

const SOURCE_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const SOURCE_IS_MANAGED_WORKTREE = resolveManagedWorktreeRoot(SOURCE_ROOT) !== null
  || resolveLinkedWorktreeLeader(SOURCE_ROOT) !== null;
const CHECK_INSTALLED_COPY = !SOURCE_IS_MANAGED_WORKTREE;
// 워크플로·프로파일 정의는 설치 가능한 패키지 안에 한 벌만 산다. 예전에는 루트에
// 같은 파일이 또 있었고, 이 스크립트가 둘의 바이트 동일성을 지켰다.
const PACKAGED_WORKFLOWS = "src/agent_flow/workflows";
const PACKAGED_PROFILES = "src/agent_flow/profiles";
const INSTALL_ROOT = resolveInstalledRoot(process.cwd()) ?? SOURCE_ROOT;
// bin/agent-flow-install.mjs / bin/agent-flow-kit.mjs의 BUNDLED_HOST_SKILL_NAMES와
// 동일해야 한다. allowlist 밖 bundled skill은 host link 없이 index에만 노출된다.
const BUNDLED_HOST_SKILL_NAMES = new Set([
  "agent-flow",
  "android-appshell-error-handling",
  "comment-authoring-discipline",
  "comment-checker",
  "ios-app-shell-error-handling",
  "react-app-shell-error-handling",
  "react-native-app-shell-error-handling",
]);
const failures = [];
const missingFiles = new Set();
const workflowExportCache = new Map();

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

// 되살아나면 안 되는 파일을 이름으로 못 박는다. 존재 검사와 대칭이라 같은 자리에 둔다.
function assertMissing(rel) {
  if (fs.existsSync(absPath(rel))) {
    failures.push(`file must not exist: ${rel}`);
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

function stripFrontmatter(text) {
  if (!text.startsWith("---\n")) {
    return text;
  }
  const end = text.indexOf("\n---\n", 4);
  return end === -1 ? text : text.slice(end + "\n---\n".length).replace(/^\n/, "");
}

function assertSameBodyAfterOptionalFrontmatter(a, b) {
  const aText = readIfExists(a);
  const bText = readIfExists(b);
  if (aText === null || bText === null) return;
  if (stripFrontmatter(aText) !== stripFrontmatter(bText)) {
    failures.push(`${a} body differs from ${b}`);
  }
}

function assertPhaseOrder(actual, expected, label) {
  let previous = -1;
  for (const phase of expected) {
    const index = actual.indexOf(phase);
    if (index === -1) {
      failures.push(`${label} missing phase ${phase}`);
      return;
    }
    if (index <= previous) {
      failures.push(`${label} expected ${expected.join(" -> ")}, got ${actual.join(" -> ")}`);
      return;
    }
    previous = index;
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

function assertAbsent(rel, needle, why) {
  const text = readIfExists(rel);
  if (text === null) return;
  if (text.includes(needle)) {
    failures.push(`${rel} must not contain ${JSON.stringify(needle)}: ${why}`);
  }
}

// 넓은 trust를 걷어내는 본문과 skill 인덱스 본문은 이제 공유 모듈에만 있다.
assertContains("lib/installer-shared.mjs", "export function removeCodexBroadTrustState(root)");
assertContains("lib/installer-shared.mjs", "export function skillIndexBlock(root)");
assertContains("lib/installer-shared.mjs", "export function upsertSkillIndexBlock(root)");
assertContains("lib/installer-shared.mjs", 'export const SKILL_INDEX_START = "<!-- agent-flow:skills:start -->"');

// `installCodexHooks`/`installClaudeHooks`/`installOmpHooks` 본문은 각 진입점의
// 전역을 읽어 아직 각자 갖고 있다. 아래 단언은 그 본문에 걸린 계약이라 두 파일을
// 모두 봐야 한다 — 한쪽만 보면 다른 쪽에서 승인 세탁이 검사 없이 되살아난다.
for (const installer of ["bin/agent-flow-kit.mjs", "bin/agent-flow-install.mjs"]) {
  assertNotContains(installer, "function installCodexTrustState(root)");
  assertContains(installer, "function installOmpHooks(root)");
  assertContains(installer, ".omp\", \"extensions\", \"agent-flow-hooks.ts");
  // 두 진입점이 같은 인덱스를 만들어야 한다. 한쪽만 채우면 install 순서에 따라
  // AGENTS.md의 인덱스가 있었다 없었다 한다.
  assertContains(installer, "upsertSkillIndexBlock(");
  // install이 **현재 등록된** hook의 해시를 trusted로 되받아 적으면, 변조된
  // 등록이 다음 install에서 승인 상태로 세탁된다. 읽는 코드도 없었다.
  // 등록 무결성은 런 시작 시 hook_integrity가 kit.json과 대조한다.
  assertAbsent(installer, "[hooks.state.", "install must not launder managed hook approval");
  assertAbsent(installer, "trusted_hash", "install must not launder managed hook approval");
  // launcher가 없으면 host의 chat 승인 hook은 실행할 것을 못 찾아 조용히 끝난다.
  // 두 진입점 중 한쪽만 심으면 그 조합에서만 승인이 죽어 재현이 안 된다.
  assertContains(installer, "installProjectLauncher(");
}
// 경로·digest 키가 JS/Python/hook 세 곳에서 같은 값인지, 그리고 hook이 실행 직전
// digest를 대조하는지. 문자열 grep만으로는 이 언어 경계가 하나도 묶이지 않는다.
assertLauncherContractIsSingleValued();

// 관리 hook 이름의 등록 지점은 Node 1곳(`lib/managed-hooks.mjs`)과 Python 1곳이다.
// 언어 경계라 합칠 수 없으므로 둘이 같은지는 계속 확인한다.
{
  const jsManagedScripts = (() => {
    const text = readIfExists("lib/managed-hooks.mjs");
    if (text === null) return null;
    const match = text.match(/const MANAGED_HOOK_SCRIPTS = \[([\s\S]*?)\];/);
    if (!match) {
      failures.push("lib/managed-hooks.mjs missing MANAGED_HOOK_SCRIPTS");
      return null;
    }
    return [...match[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]).sort();
  })();
  if (jsManagedScripts && jsManagedScripts.length === 0) {
    failures.push("lib/managed-hooks.mjs declares no managed hook scripts");
  }
}

const fullFeatureWorkflowCopies = [
  `${PACKAGED_WORKFLOWS}/full-feature.yaml`,
  ...(CHECK_INSTALLED_COPY ? [".agent-flow/workflows/full-feature.yaml"] : []),
];

const exportedWorkflow = workflowExport("full-feature");
if (exportedWorkflow) {
  const exportedPhases = Object.fromEntries(exportedWorkflow.phases.map((phase) => [phase.id, phase]));
  const fullFeatureOrder = exportedWorkflow.phases.map((phase) => phase.id);
  if (exportedWorkflow.id !== "full-feature") {
    failures.push("workflow export full-feature id mismatch");
  }
  assertPhaseOrder(
    fullFeatureOrder,
    ["refactor", "comment-authoring", "multi-review", "architecture-review", "gates", "fix-loop", "commit"],
    "full-feature review-before-QA order",
  );
  if (exportedPhases["domain-grill"]?.artifact !== "artifacts/domain-grill.md") {
    failures.push("workflow export domain-grill artifact mismatch");
  }
  if (exportedPhases["gates"]?.routes?.green !== "commit") {
    failures.push("workflow export gates green route mismatch");
  }
  if (exportedPhases["gates"]?.artifact !== "artifacts/gate-results.json") {
    failures.push("workflow export gates artifact mismatch");
  }
  if (exportedPhases["comment-authoring"]?.routes?.default !== "multi-review") {
    failures.push("workflow export comment-authoring route mismatch");
  }
  if (exportedPhases["red"]?.artifact !== "artifacts/red.log") {
    failures.push("workflow export red artifact mismatch");
  }
  if (exportedPhases["architecture-review"]?.routes?.approve !== "gates") {
    failures.push("workflow export architecture-review approve route mismatch");
  }
  if (exportedPhases["architecture-review"]?.routes?.["request-changes"] !== "refactor") {
    failures.push("workflow export architecture-review request-changes route mismatch");
  }
  if (Object.prototype.hasOwnProperty.call(exportedPhases["architecture-review"]?.routes ?? {}, "blocked")) {
    failures.push("workflow export architecture-review blocked route should be absent");
  }
  if (exportedPhases["merge-approval"]?.routes?.approve !== "merge") {
    failures.push("workflow export merge-approval approve route mismatch");
  }
  if (exportedPhases["merge-approval"]?.routes?.default !== "block") {
    failures.push("workflow export merge-approval default route mismatch");
  }
  if (exportedPhases["fix-loop"]?.routes?.default !== "comment-authoring") {
    failures.push("workflow export fix-loop route mismatch");
  }
  if (exportedPhases["multi-review"]?.multi_review !== true) {
    failures.push("workflow export multi-review multi_review flag mismatch");
  }
  if (exportedPhases["pr-watch"]?.routes?.merged !== "handoff") {
    failures.push("workflow export pr-watch merged route mismatch");
  }
  if (exportedPhases["pr-watch"]?.routes?.skipped !== "handoff") {
    failures.push("workflow export pr-watch skipped route mismatch");
  }
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
  if (exportedPhases["final-review"]?.routes?.approve !== "gates") {
    failures.push("workflow export default final-review approve route mismatch");
  }
  if (exportedPhases["final-review"]?.routes?.["request-changes"] !== "fix-loop") {
    failures.push("workflow export default final-review request-changes route mismatch");
  }
  if (exportedPhases["final-review"]?.multi_review !== true) {
    failures.push("workflow export default final-review multi_review flag mismatch");
  }
  if (exportedPhases["fix-loop"]?.routes?.default !== "comment-authoring") {
    failures.push("workflow export default fix-loop route mismatch");
  }
  if (exportedPhases["pr-watch"]?.routes?.merged !== "cleanup") {
    failures.push("workflow export default pr-watch merged route mismatch");
  }
  if (exportedPhases["pr-watch"]?.routes?.skipped !== "cleanup") {
    failures.push("workflow export default pr-watch skipped route mismatch");
  }
}

assertCodeReviewerCoversWorkflowMarkers("default", "final-review");
assertCodeReviewerCoversWorkflowMarkers("full-feature", "multi-review");
assertCodeReviewerCoversWorkflowMarkers("full-feature", "architecture-review");

assertAllWorkflowContracts();
// 같은 git discovery 변수 목록이 7군데에 복사되어 있고 지금까지는 주석만 "같아야
// 한다"고 말했다. 한 군데만 밀려도 그 진입점에서만 ambient GIT_*가 살아남아 남의
// checkout을 보고, 증상은 전혀 다른 곳에서 터진다. 실제로 목록을 추출해 강제한다.
assertLeakyGitEnvParity();
assertContains("bin/agent-flow-kit.mjs", "RUNTIME_PYTHON_RELATIVE");
assertContains("bin/agent-flow-kit.mjs", "src\", \"agent_flow");
assertContains("src/agent_flow/core/gates.py", "\".agent-flow\" / \"runtime\" / \"python\"");
// artifact 경로 정규화는 한 벌만 존재해야 한다. artifacts.py가 자기 규칙을 다시
// 쓰면 command와 gate 출력이 서로 다른 경로를 기록한다.
assertContains("src/agent_flow/core/gates.py", "def relativize_local_path");
assertContains("src/agent_flow/core/artifacts.py", "relativize_local_paths");
// 설치본 낡음 경고는 두 진입점에 다 있다. 지문이 갈라지면 한쪽만 경고하거나
// 한쪽이 자산 변경 없이 오경고한다.
assertPythonContract("kit source digest matches the node wrapper", `
import hashlib
from pathlib import Path

from agent_flow.core.kit_digest import KIT_SOURCE_DIGEST_ROOTS, kit_source_digest

expected = ${JSON.stringify(nodeKitSourceDigest())}
actual = kit_source_digest(Path(${JSON.stringify(SOURCE_ROOT)}))
if actual != expected:
    raise AssertionError(f"python {actual} != node {expected}")
`);

function nodeKitSourceDigest() {
  const source = read("bin/agent-flow-kit.mjs");
  const start = source.indexOf("const KIT_SOURCE_DIGEST_ROOTS");
  const end = source.indexOf("\n}\n", source.indexOf("function walkFilesSorted")) + 3;
  if (start === -1 || end <= start) {
    failures.push("node kit source digest extraction failed");
    return "";
  }
  return new Function(
    "fs",
    "path",
    "crypto",
    "KIT_ROOT",
    `${source.slice(start, end)}\nreturn kitSourceDigest();`,
  )(fs, path, { createHash }, SOURCE_ROOT);
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
  assertContains(rel, "Reviewers are installed Claude and Codex CLIs only");
  assertNotContains(rel, "Gemini sub-agent in Gemini");
  assertContains(rel, "reviewer-source: sub-agent");
  assertContains(rel, "Never use OMP or controller-session work");
  assertContains(rel, "## Overall");
  assertContains(rel, "verdict: approve");
  assertContains(rel, "verdict: request-changes");
  assertContains(rel, "multi_review: true");
  assertNotContains(rel, "id: domain-map");
  assertNotContains(rel, "grill-me");
}

// 정의는 패키지 안에 한 벌만 있다. 예전에는 루트 사본과 바이트 비교를 했지만
// 비교할 두 번째 사본이 없어졌다. 설치본이 정본에서 밀리지 않았는지만 본다.
if (CHECK_INSTALLED_COPY) {
  assertSameYamlFileSet(PACKAGED_WORKFLOWS, ".agent-flow/workflows");
  for (const entry of fs.readdirSync(path.join(SOURCE_ROOT, PACKAGED_WORKFLOWS)).sort()) {
    if (!entry.endsWith(".yaml")) continue;
    assertSame(`${PACKAGED_WORKFLOWS}/${entry}`, `.agent-flow/workflows/${entry}`);
  }
}
assertContains(`${PACKAGED_WORKFLOWS}/default.yaml`, "installed Claude and Codex CLIs only");
assertNotContains(`${PACKAGED_WORKFLOWS}/default.yaml`, "Gemini sub-agent");
assertContains(`${PACKAGED_WORKFLOWS}/default.yaml`, "reviewer-source: sub-agent");
assertContains(`${PACKAGED_WORKFLOWS}/default.yaml`, "Never use OMP or controller-session work");
assertContains(`${PACKAGED_WORKFLOWS}/default.yaml`, "## Overall");
assertContains(`${PACKAGED_WORKFLOWS}/default.yaml`, "verdict: approve");
assertContains(`${PACKAGED_WORKFLOWS}/default.yaml`, "verdict: request-changes");
assertContains(`${PACKAGED_WORKFLOWS}/default.yaml`, "id: comment-authoring");
assertContains(`${PACKAGED_WORKFLOWS}/default.yaml`, "`n/a` only when the changed diff has no");
assertContains(`${PACKAGED_WORKFLOWS}/default.yaml`, "comment-scope: final-pass-only");
assertContains("skills/code-generation-discipline/SKILL.md", "Write comments only when code alone cannot carry the reason or contract.");
assertNotContains("skills/code-generation-discipline/SKILL.md", "Every new or modified code block must include Korean " + "comments");
if (CHECK_INSTALLED_COPY) {
  assertContains(".agent-flow/prompts/multi-review.md", "Reviewers are installed Claude and Codex CLIs only");
  assertContains(".agent-flow/prompts/multi-review.md", "skills_checked: true");
  assertNotContains(".agent-flow/prompts/multi-review.md", "Gemini sub-agent");
  assertContains(".agent-flow/prompts/multi-review.md", "reviewer-source: sub-agent");
  assertContains(".agent-flow/prompts/multi-review.md", "Never use OMP or controller-session work");
  assertContains(".agent-flow/prompts/multi-review.md", "## Overall");
  assertContains(".agent-flow/prompts/multi-review.md", "verdict: approve");
  assertContains(".agent-flow/prompts/multi-review.md", "verdict: request-changes");
  assertNotContains(`${PACKAGED_WORKFLOWS}/full-feature.yaml`, "Gemini sub-agent");
  assertNotContains("bootstrap/AGENTS.md.template", "Gemini sub-agent");
}

// skill source와 설치본이 달라지면 다른 프로젝트로 전파될 때 기준이 갈린다.
if (CHECK_INSTALLED_COPY) {
  for (const skill of [
    "code-review",
    "codebase-design",
    "domain-modeling",
    "grill-with-docs",
    "grilling",
    "tdd",
    "to-prd",
  ]) {
    for (const rel of recursiveFiles(`skills/${skill}`)) {
      assertSame(rel, `.agent-flow/${rel}`);
    }
  }
  assertSame("skills/agent-flow/SKILL.md", ".agent-flow/skills/agent-flow/SKILL.md");
  assertSame("skills/code-generation-discipline/SKILL.md", ".agent-flow/skills/code-generation-discipline/SKILL.md");
}

function installedKitPayload() {
  const text = readIfExists(".agent-flow/kit.json");
  if (text === null) {
    return null;
  }
  try {
    const payload = JSON.parse(text);
    if (payload && typeof payload === "object") {
      return payload;
    }
    failures.push(".agent-flow/kit.json is not an object");
    return null;
  } catch {
    // 조용히 null로 넘기면 아래 keep-set이 최소 집합으로 쪼그라들고, 정상 선택된
    // profile이 비교 대상에서 빠져 바이트 동일성 검사가 통째로 꺼진다.
    failures.push(".agent-flow/kit.json is not valid JSON");
    return null;
  }
}

// 설치본은 이 프로젝트의 stack + generic + _schema만 싣는다. 전체 집합을 요구하면
// 정상 설치가 "빠진 profile"로 잡힌다.
const INSTALLED_KIT = installedKitPayload();
const INSTALLED_PROFILE_FILES = installedProfileFileNames(
  activeInstallProfileIds(INSTALLED_KIT?.profile, INSTALLED_KIT),
);

if (CHECK_INSTALLED_COPY) {
  const packagedProfiles = new Set(yamlFileNames(PACKAGED_PROFILES));
  const installedProfiles = new Set(yamlFileNames(".agent-flow/profiles"));
  for (const file of INSTALLED_PROFILE_FILES) {
    if (!installedProfiles.has(file)) {
      failures.push(`.agent-flow/profiles missing ${file} from ${PACKAGED_PROFILES}`);
    }
  }
  for (const file of installedProfiles) {
    // 배포 이름이 아닌 것은 사용자가 만든 custom profile이다. install이 그것을
    // 보존하는 것이 계약이므로 여기서 실패로 잡으면 오탐이다.
    if (packagedProfiles.has(file) && !INSTALLED_PROFILE_FILES.has(file)) {
      failures.push(`.agent-flow/profiles has unselected ${file}`);
    }
  }
}

if (CHECK_INSTALLED_COPY) {
  assertSameRelativeFileSet("templates", ".agent-flow/templates");
  for (const rel of recursiveFiles("templates")) {
    assertSame(rel, `.agent-flow/${rel}`);
  }
}
for (const entry of fs.readdirSync(path.join(SOURCE_ROOT, PACKAGED_PROFILES)).sort()) {
  if (!entry.endsWith(".yaml")) continue;
  // 이 프로젝트가 고르지 않은 stack은 설치본에 없다. 그 자리는 비교 대상이
  // 아니라 계약대로 빠진 것이다.
  if (!CHECK_INSTALLED_COPY || !INSTALLED_PROFILE_FILES.has(entry)) {
    continue;
  }
  // 바이트 동일성이 gate 목록 동일성을 포함한다. gate id를 따로 비교하면 같은
  // drift를 두 줄로 보고할 뿐이다.
  assertSame(`${PACKAGED_PROFILES}/${entry}`, `.agent-flow/profiles/${entry}`);
}

// #105: profile skill 표는 소비자가 있어야 한다. 선언만 남으면 "보여 주지 않는
// 표를 읽으라"로 되돌아간다.
{
  const resolverText = readIfExists("src/agent_flow/core/skill_resolver.py") ?? "";
  if (!resolverText.includes("routed_profile_skills(")) {
    failures.push("skill_resolver.py must consume profile_routing.routed_profile_skills");
  }
  const routingText = readIfExists("src/agent_flow/core/profile_routing.py");
  if (routingText === null) {
    failures.push("src/agent_flow/core/profile_routing.py is missing");
  }
  // Node runner는 skill 판정을 Python CLI에 위임한다. 여기에 matcher가 생기면
  // 두 벌이 조용히 갈라진다.
  const kitText = readIfExists("bin/agent-flow-kit.mjs") ?? "";
  if (kitText.includes("task_terms")) {
    failures.push("bin/agent-flow-kit.mjs must not reimplement profile skill routing");
  }
  for (const entry of fs.readdirSync(path.join(SOURCE_ROOT, PACKAGED_PROFILES)).sort()) {
    if (!entry.endsWith(".yaml") || entry.startsWith("_")) continue;
    const text = readIfExists(`${PACKAGED_PROFILES}/${entry}`) ?? "";
    if (/^\s*(android_skills|chrisbanes_skills):\s*$/m.test(text)) {
      failures.push(
        `${PACKAGED_PROFILES}/${entry}: external skill names must not be enumerated in a profile table`,
      );
    }
    for (const [name, block] of externalDomainsWithoutTerms(text)) {
      if (!/\n\s+terms:/.test(block)) {
        failures.push(
          `${PACKAGED_PROFILES}/${entry}: external domain ${name} has no terms and can never activate`,
        );
      }
    }
  }
}

// 어휘 없는 domain은 활성화될 수 없다. 선언만 남아 아무 일도 하지 않는 상태를 막는다.
function externalDomainsWithoutTerms(text) {
  const lines = text.split(/\r?\n/);
  const found = [];
  let inDomains = false;
  let indent = "";
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const opener = line.match(/^(\s+)domains:\s*$/);
    if (opener) {
      inDomains = true;
      indent = opener[1];
      continue;
    }
    if (inDomains && /^\S/.test(line)) inDomains = false;
    if (!inDomains) continue;
    const match = line.match(/^(\s+)- id: (\S+)\s*$/);
    if (!match || match[1].length <= indent.length) continue;
    const body = [];
    for (let cursor = index + 1; cursor < lines.length; cursor += 1) {
      const next = lines[cursor];
      if (!next.startsWith(`${match[1]}  `) || /^\s+- /.test(next)) break;
      body.push(next);
    }
    found.push([match[2], `\n${body.join("\n")}`]);
  }
  return found;
}

// bootstrap은 반복 install 대신 기존 설치된 CLI로 worktree run을 시작해야 한다.
//
// 블록의 정본은 `bootstrap/AGENTS.md.template` 한 벌이고, 루트 `AGENTS.md`와
// `CLAUDE.md`는 그 한 벌을 받는다. 예전에는 label마다 템플릿이 따로 있었다:
// 처음에는 두 벌의 바이트 동일성을 요구했고(같은 본문을 두 벌 유지하라는 요구),
// 다음에는 `CLAUDE.md.template`을 `@AGENTS.md` **포인터만** 담은 파일로 바꿨다.
// 포인터 하나만 두면 Claude는 계약을 import로 받고 Codex/OMP는 직접 읽어 host마다
// 로드되는 텍스트가 갈렸다. 지금은 둘 다 같은 블록을 받고, import는 블록 **밖**
// 프로젝트 산문을 Claude에게 전달하는 한 줄로만 남는다(`rootBootstrapBlock`).
// 여기서는 label별 템플릿이 되살아나지 않았는지를 본다.
assertMissing("bootstrap/CLAUDE.md.template");
assertContains("bootstrap/AGENTS.md.template", "<!-- agent-flow:end -->");
// 템플릿 자신은 import를 담지 않는다. 담으면 `AGENTS.md`가 자기 자신을 import한다.
assertNotContains("bootstrap/AGENTS.md.template", "@AGENTS.md");

// 두 installer 모두 그 한 벌을 읽는다. 예전에 `agent-flow-kit.mjs`는 같은 텍스트를
// 리터럴로 또 들고 있었고, 신규 install은 템플릿을 쓰고 이후 kit install은 리터럴을 써서
// 같은 프로젝트의 AGENTS.md가 어느 쪽이 마지막으로 돌았는지에 따라 달라졌다. parity가
// 그 리터럴을 보지 않아 드리프트를 잡지도 못했다 — 아래 단언들이 그 회귀의 오라클이다.
//
// 이제 `.agent-flow/bootstrap/<label>` 관리 사본도 같은 템플릿에서 파생된다. 그래서
// 섹션 제목까지 금지할 수 있다: 예전에는 사본 생성기가 그 제목을 정당하게 리터럴로
// 들고 있어 제목이 오라클이 못 됐고, 정확히 그 자리에서 사본이 갈라졌다.
for (const installer of ["bin/agent-flow-kit.mjs", "bin/agent-flow-install.mjs"]) {
  assertContains(installer, 'path.join(KIT_ROOT, "bootstrap", BOOTSTRAP_TEMPLATE_FILE)');
  // label이 고르는 것은 이 한 줄뿐이다. 생성기가 두 벌이 되면 CLAUDE.md만 import를
  // 잃는 조합이 다시 가능해진다.
  assertContains(installer, "rootBootstrapBlock(label, ");
  assertNotContains(installer, "`${label}.template`");
  assertNotContains(installer, "<!-- agent-flow:skills:start -->");
  assertNotContains(installer, "## Agent Flow");
  assertNotContains(installer, "### Workflow Contract");
  assertNotContains(installer, "### Context Economy");
}

if (CHECK_INSTALLED_COPY) {
  // `.agent-flow/bootstrap/*`는 루트 블록이 아니라 관리 사본 생성기의 산출물이다.
  // 두 label이 제목만 다른 한 벌이라서 여기서는 동일성이 그대로 성립한다.
  assertSameBodyAfterTitle(".agent-flow/bootstrap/AGENTS.md", ".agent-flow/bootstrap/CLAUDE.md");
}
for (const rel of [
  "bootstrap/AGENTS.md.template",
  ...(CHECK_INSTALLED_COPY ? [
    ".agent-flow/bootstrap/AGENTS.md",
    ".agent-flow/bootstrap/CLAUDE.md",
  ] : []),
]) {
  assertFile(rel);
  assertContains(rel, 'agent-flow run "<task>"');
  assertContains(rel, "agent-flow status");
  assertContains(rel, "install은 프로젝트당 1회만");
  assertContains(rel, "next_command");
  assertContains(rel, "짧은 한글");
  assertContains(rel, "설치된 Claude/Codex CLI reviewer subprocess 2개 이상이 필수");
  assertContains(rel, "OMP는 host/controller로만 쓰고 reviewer provider로는 쓰지 않는다");
  assertNotContains(rel, "예: Claude/Gemini");
  assertContains(rel, "reviewer-source: sub-agent");
  assertNotContains(rel, "활성 host");
  assertContains(rel, "## Overall");
  assertContains(rel, "verdict: approve");
  assertContains(rel, "verdict: request-changes");
  assertContains(rel, "보호 브랜치 commit/push와 leader checkout/switch 금지는 모든 host에서 동일");
}

// 실측된 마찰 셋을 문구로 못 박는다. 이것이 빠지면 agent는 3-6 phase짜리 짧은 workflow의
// 존재를 모른 채 15-phase `default`만 쓰고, leader에서 빌드를 돌려 phase 경계 tripwire를
// 깨고, gate에 없는 검증 명령을 반복한다.
for (const needle of [
  "--workflow <name>",
  "leader checkout에서 IDE·Gradle·build를 실행하지 않는다",
  "build/test/lint 명령은 active profile의 `gates`에서만 가져온다",
]) {
  assertContains("bootstrap/AGENTS.md.template", needle);
}

// phase 수는 문자열로 지키면 조용히 거짓이 된다: 지금 값은 맞지만 phase가 하나만 늘어도
// 템플릿은 그대로고 검사도 통과한다. 정본인 yaml에서 세어 기대 문자열을 조립한다.
// 목록을 yaml 파일 집합에서 돌리므로 workflow를 새로 추가하면 템플릿에 그것을 적기
// 전까지 parity가 막는다 — 이름 목록을 여기에 다시 적지 않는 이유다.
for (const name of yamlFileNames(PACKAGED_WORKFLOWS).map((file) => file.replace(/\.yaml$/, ""))) {
  const workflow = workflowExport(name);
  if (!workflow) continue;
  assertContains("bootstrap/AGENTS.md.template", `\`${name}\`(${workflow.phases.length})`);
}

assertFile(".Codex/agents/code-reviewer.md");
assertFile(".claude/agents/code-reviewer.md");
assertSameBodyAfterOptionalFrontmatter(".Codex/agents/code-reviewer.md", ".claude/agents/code-reviewer.md");
assertContains(".claude/agents/code-reviewer.md", "name: code-reviewer");
assertContains(".claude/agents/code-reviewer.md", "description:");

if (CHECK_INSTALLED_COPY) {
  assertContains(".agent-flow/rules/workflow-contract.md", "Required review happens before completion QA");
  assertContains(".agent-flow/rules/workflow-contract.md", "agent-flow gates --phase all");
  assertContains(".agent-flow/rules/workflow-contract.md", "default workflow, gates run as their own phase");
  assertContains(".agent-flow/rules/workflow-contract.md", "short Korean");
  assertContains(".agent-flow/rules/workflow-contract.md", "two independent Claude/Codex reviewer subprocesses");
  assertNotContains(".agent-flow/rules/workflow-contract.md", "Gemini sub-agent in Gemini");
  assertContains(".agent-flow/rules/workflow-contract.md", "reviewer-source: sub-agent");
  assertContains(".agent-flow/rules/workflow-contract.md", "OMP and controller-session work are never reviewer providers");
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
  return resolveManagedWorktreeContext(start)?.root ?? null;
}

function resolveGitCommonWorktreeRoot(start) {
  const topLevel = gitOutput(start, ["rev-parse", "--show-toplevel"]);
  const commonDir = gitOutput(start, ["rev-parse", "--git-common-dir"]);
  if (!topLevel || !commonDir) {
    return null;
  }
  // --git-common-dir은 cwd 기준 상대경로를 낸다. topLevel 기준으로 풀면 cwd가
  // 하위 디렉토리일 때 어긋난다. 두 installer의 같은 함수와 기준을 맞춘다.
  const resolvedCommonDir = path.resolve(start, commonDir);
  if (path.basename(resolvedCommonDir) !== ".git") {
    return null;
  }
  return path.dirname(resolvedCommonDir);
}

function gitEnv() {
  const env = { ...process.env };
  for (const name of LEAKY_GIT_ENV_VARS) {
    delete env[name];
  }
  return env;
}

function gitOutput(cwd, args) {
  const result = spawnSync("git", args, {
    cwd,
    env: gitEnv(),
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
  if (workflowExportCache.has(name)) {
    return workflowExportCache.get(name);
  }
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
    workflowExportCache.set(name, null);
    return null;
  }
  try {
    const payload = JSON.parse(result.stdout);
    if (!Array.isArray(payload.phases)) {
      failures.push("workflow export phases must be an array");
      workflowExportCache.set(name, null);
      return null;
    }
    workflowExportCache.set(name, payload);
    return payload;
  } catch (error) {
    failures.push(`workflow export invalid JSON: ${error.message}`);
    workflowExportCache.set(name, null);
    return null;
  }
}

function assertCodeReviewerCoversWorkflowMarkers(workflowName, phaseId) {
  const workflow = workflowExport(workflowName);
  if (!workflow) return;
  const phase = workflow.phases.find((item) => item.id === phaseId);
  if (!phase) {
    failures.push(`workflow export ${workflowName} missing ${phaseId}`);
    return;
  }
  for (const reviewerRel of [".Codex/agents/code-reviewer.md", ".claude/agents/code-reviewer.md"]) {
    const reviewerText = readIfExists(reviewerRel);
    if (reviewerText === null) continue;
    const reviewerMarkerKeys = new Set(
      stripFrontmatter(reviewerText)
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter((line) => /^[A-Za-z0-9_-]+:\s+/.test(line))
        .map(markerKey),
    );
    for (const marker of phase.required_markers ?? []) {
      const key = markerKey(marker);
      if (key && !reviewerMarkerKeys.has(key)) {
        failures.push(`${reviewerRel} missing marker for ${workflowName}:${phaseId}: ${marker}`);
      }
    }
  }
}

function markerKey(marker) {
  return String(marker).split(":", 1)[0].trim();
}

function assertPythonContract(label, code) {
  const env = {
    ...process.env,
    PYTHONPATH: [path.join(SOURCE_ROOT, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
  };
  const result = spawnSync(preferredPython(), ["-c", code], {
    cwd: SOURCE_ROOT,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    env,
    timeout: 30_000,
  });
  if (result.error || result.status !== 0) {
    failures.push(`${label} failed: ${result.error?.message || result.stderr.trim() || result.status}`);
  }
}

// 나열 순서는 파일마다 자유다(집합만 같아야 한다). 심볼 이름도 파일마다 다르므로
// 파일별로 어느 선언을 볼지 명시한다.
function assertLeakyGitEnvParity() {
  const declarations = [
    ["src/agent_flow/core/worktree_isolation.py", "LEAKY_GIT_ENV_VARS"],
    ["lib/installer-shared.mjs", "LEAKY_GIT_ENV_VARS"],
    ["scripts/check-agent-flow-parity.mjs", "LEAKY_GIT_ENV_VARS"],
    ["lib/omp-hooks-extension.mjs", "GIT_DISCOVERY_ENV"],
    ["scripts/hooks/record-skill-read.py", "LEAKY_GIT_ENV_VARS"],
    ["scripts/hooks/record-command-run.py", "LEAKY_GIT_ENV_VARS"],
  ];
  const extracted = [];
  for (const [rel, symbol] of declarations) {
    const names = leakyGitEnvNames(rel, symbol);
    if (names === null) {
      continue;
    }
    if (names.length === 0) {
      // 선언을 못 찾으면 "일치함"으로 넘어가면 안 된다. 이름을 바꾼 쪽이
      // 검사에서 조용히 빠지는 게 정확히 이 검사가 막으려는 상황이다.
      failures.push(`${rel} ${symbol} declaration not found`);
      continue;
    }
    extracted.push([rel, new Set(names)]);
  }
  if (extracted.length < 2) {
    return;
  }
  const [baseRel, baseNames] = extracted[0];
  for (const [rel, names] of extracted.slice(1)) {
    const missing = [...baseNames].filter((name) => !names.has(name));
    const extra = [...names].filter((name) => !baseNames.has(name));
    if (missing.length > 0 || extra.length > 0) {
      failures.push(`${rel} git discovery env list differs from ${baseRel}: missing=${missing.join(",") || "<none>"} extra=${extra.join(",") || "<none>"}`);
    }
  }
}

function leakyGitEnvNames(rel, symbol) {
  const text = readIfExists(rel);
  if (text === null) {
    return null;
  }
  // JS 배열과 Python 튜플을 같은 규칙으로 읽는다. 목록 안에는 중첩 괄호가 없어
  // 첫 닫는 괄호까지 잘라도 안전하다.
  const match = new RegExp(String.raw`\b${symbol}\s*=\s*[[(]([^)\]]*)[)\]]`).exec(text);
  if (!match) {
    return [];
  }
  return [...match[1].matchAll(/"([A-Z_]+)"/g)].map((entry) => entry[1]);
}

function assertAllWorkflowContracts() {
  for (const name of yamlFileNames(PACKAGED_WORKFLOWS).map((file) => file.replace(/\.yaml$/, ""))) {
    const workflow = workflowExport(name);
    if (!workflow) {
      continue;
    }
    if (workflow.id !== name) {
      failures.push(`workflow export ${name} id mismatch: ${workflow.id}`);
    }
    assertWorkflowArtifactContract(workflow);
  }
}

function assertWorkflowArtifactContract(workflow) {
  const phaseIds = new Set();
  for (const phase of workflow.phases) {
    if (phaseIds.has(phase.id)) {
      failures.push(`workflow ${workflow.id} duplicate phase id ${phase.id}`);
    }
    phaseIds.add(phase.id);
  }
  for (const phase of workflow.phases) {
    if (typeof phase.artifact !== "string" || phase.artifact.length === 0) {
      failures.push(`workflow ${workflow.id} phase ${phase.id} missing artifact`);
      continue;
    }
    if (path.isAbsolute(phase.artifact) || phase.artifact.split(/[\\/]+/).includes("..")) {
      failures.push(`workflow ${workflow.id} phase ${phase.id} unsafe artifact ${phase.artifact}`);
    }
    if (!/\.(md|json|log)$/.test(phase.artifact)) {
      failures.push(`workflow ${workflow.id} phase ${phase.id} unsupported artifact extension ${phase.artifact}`);
    }
    const outputs = promptOutputArtifacts(phase.prompt ?? phase.instruction ?? "");
    if (outputs.length > 0 && !outputs.includes(phase.artifact)) {
      failures.push(`workflow ${workflow.id} phase ${phase.id} output/artifact mismatch: artifact=${phase.artifact} outputs=${outputs.join(",")}`);
    }
    for (const target of Object.values(phase.routes ?? {})) {
      if (target !== "block" && !phaseIds.has(target)) {
        failures.push(`workflow ${workflow.id} phase ${phase.id} route targets unknown phase ${target}`);
      }
    }
  }
}

function promptOutputArtifacts(prompt) {
  return [...prompt.matchAll(/Output:\s+`?([A-Za-z0-9_./-]+\.(?:md|json|log))`?\.?/g)]
    .map((match) => match[1]);
}

function assertCleanInstallCopiesTemplates() {
  for (const installer of ["agent-flow-kit.mjs", "agent-flow-install.mjs"]) {
    assertInstallerCleanInstallCopiesTemplates(installer);
    assertInstallerSelfInstallKeepsSourceScripts(installer);
  }
}

function assertInstallerSelfInstallKeepsSourceScripts(installer) {
  const label = `bin/${installer}`;
  const tempParent = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-self-install-parity-"));
  const tempKitRoot = path.join(tempParent, "kit");
  try {
    fs.cpSync(SOURCE_ROOT, tempKitRoot, {
      recursive: true,
      filter: (source) => {
        const rel = path.relative(SOURCE_ROOT, source);
        const parts = rel.split(path.sep);
        return !parts.some((part) =>
          [".agent-flow", ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__", "node_modules"].includes(part),
        );
      },
    });
    const sourceChecker = path.join(tempKitRoot, "scripts", "hooks", "comment-checker.py");
    if (!fs.existsSync(sourceChecker)) {
      failures.push(`${label} self install fixture missing source scripts/hooks/comment-checker.py`);
      return;
    }
    const result = spawnSync(process.execPath, [path.join(tempKitRoot, "bin", installer), "install", "--force-managed"], {
      cwd: tempKitRoot,
      env: { ...process.env, AGENT_FLOW_SKIP_CODEX_TRUST: "1" },
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
      timeout: 30_000,
    });
    if (result.error || result.status !== 0) {
      failures.push(`${label} self install parity failed: ${result.error?.message || result.stderr.trim() || result.status}`);
      return;
    }
    if (!fs.existsSync(sourceChecker)) {
      failures.push(`${label} self install removed source scripts/hooks/comment-checker.py`);
    }
    if (!fs.existsSync(path.join(tempKitRoot, ".agent-flow", "scripts", "hooks", "comment-checker.py"))) {
      failures.push(`${label} self install missing .agent-flow/scripts/hooks/comment-checker.py`);
    }
  } finally {
    fs.rmSync(tempParent, { recursive: true, force: true });
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
      env: { ...process.env, AGENT_FLOW_SKIP_CODEX_TRUST: "1" },
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
    const installedChecker = path.join(tempRoot, ".agent-flow", "scripts", "hooks", "comment-checker.py");
    for (const [rel, installedHooks] of [
      [".Codex/hooks.json", path.join(tempRoot, ".Codex", "hooks.json")],
      [".codex/hooks.json", path.join(tempRoot, ".codex", "hooks.json")],
    ]) {
      if (!fs.existsSync(installedHooks)) {
        failures.push(`${label} clean install missing ${rel}`);
        continue;
      }
      const hooksText = fs.readFileSync(installedHooks, "utf8");
      if (!hooksText.includes("comment-checker.py")) {
        failures.push(`${label} clean install ${rel} missing comment-checker hook`);
      }
      if (!hooksText.includes("custom-post-hook")) {
        failures.push(`${label} clean install ${rel} did not preserve existing custom hook`);
      }
      if (!hooksText.includes(installedChecker)) {
        failures.push(`${label} clean install ${rel} does not use installed comment-checker`);
      }
      if (hooksText.includes(SOURCE_ROOT)) {
        failures.push(`${label} clean install ${rel} leaks source root`);
      }
    }
    if (!fs.existsSync(installedChecker)) {
      failures.push(`${label} clean install missing .agent-flow/scripts/hooks/comment-checker.py`);
    } else {
      try {
        fs.accessSync(installedChecker, fs.constants.X_OK);
      } catch {
        failures.push(`${label} clean install comment-checker.py is not executable`);
      }
    }
    if (fs.existsSync(path.join(tempRoot, "scripts", "hooks", "comment-checker.py"))) {
      failures.push(`${label} clean install duplicated legacy scripts/hooks/comment-checker.py`);
    }
    const claudeSettingsPath = path.join(tempRoot, ".claude", "settings.json");
    if (!fs.existsSync(claudeSettingsPath)) {
      failures.push(`${label} clean install missing .claude/settings.json`);
    } else {
      const claudeSettingsText = fs.readFileSync(claudeSettingsPath, "utf8");
      if (!claudeSettingsText.includes("PostToolUse") || !claudeSettingsText.includes("comment-checker.py")) {
        failures.push(`${label} clean install .claude/settings.json missing comment-checker PostToolUse hook`);
      }
      if (!claudeSettingsText.includes(installedChecker)) {
        failures.push(`${label} clean install .claude/settings.json does not use installed comment-checker`);
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
      env: { ...process.env, AGENT_FLOW_SKIP_CODEX_TRUST: "1" },
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
    for (const host of ["claude", "codex", "omp"]) {
      if (fs.existsSync(hostSkillFile(tempRoot, host, "demo-stale"))) {
        failures.push(`${label} force install left previous-index stale ${host} skill link`);
      }
    }
    for (const name of [
      "architecture-reviewer",
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
    assertInstalledHookParity(label, tempRoot);
    assertSkillIndexComplete(label, tempRoot);
    assertSkillIndexBlockMatchesInstall(label, tempRoot);
    assertInstallerWorkflowBackupAndTimestamps(label, tempRoot, installer);
    assertInstalledBootstrapDerivesFromTemplate(label, tempRoot);
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true });
  }
}

// 관리 사본(`.agent-flow/bootstrap/<label>`)과 루트 블록이 같은 템플릿에서 나오는지를
// **설치 결과**로 확인한다. 소스 리터럴 금지만으로는 부족하다: 사본 생성기가 템플릿을
// 읽으면서도 임의로 문장을 덧대거나 골라 넣으면 그 검사는 통과하고 사본은 다시 갈라진다.
//
// 기대값을 여기서 다시 파생시키는 것은 의도적인 이중 구현이다. 규칙(제목 한 줄 + 마커를
// 뗀 정본 본문)이 짧아 독립 오라클로 유지할 수 있고, installer와 같은 코드를 부르면
// 잘못된 규칙을 스스로 승인하게 된다.
function assertInstalledBootstrapDerivesFromTemplate(label, tempRoot) {
  const template = readIfExists("bootstrap/AGENTS.md.template");
  if (template === null) return;
  const body = [];
  let insideSkillIndex = false;
  for (const line of template.split(/\r?\n/)) {
    const marker = /^<!--\s*agent-flow:([a-z:]*)\s*-->$/.exec(line.trim());
    if (marker) {
      if (marker[1] === "skills:start") insideSkillIndex = true;
      else if (marker[1] === "skills:end") insideSkillIndex = false;
      continue;
    }
    if (insideSkillIndex) continue;
    body.push(line);
  }
  const derived = body.join("\n").trim();
  for (const managedLabel of ["AGENTS.md", "CLAUDE.md"]) {
    const managedPath = path.join(tempRoot, ".agent-flow", "bootstrap", managedLabel);
    if (!fs.existsSync(managedPath)) {
      failures.push(`${label} install missing .agent-flow/bootstrap/${managedLabel}`);
      continue;
    }
    const expected = `# ${managedLabel} Agent Flow Bootstrap\n\n${derived}\n`;
    if (fs.readFileSync(managedPath, "utf8") !== expected) {
      failures.push(`${label} .agent-flow/bootstrap/${managedLabel} is not derived from bootstrap/AGENTS.md.template`);
    }
  }
  // 루트 파일은 블록을 그대로 받는다. 사본 쪽과 달리 마커가 살아 있어야 재설치가
  // 멱등하다 — 마커가 없으면 다음 install이 블록을 찾지 못하고 뒤에 또 붙인다.
  //
  // 두 루트 파일이 같은 블록을 받는지도 여기서 본다. host마다 읽는 파일이 다르므로
  // (Claude는 `CLAUDE.md`, Codex/OMP는 `AGENTS.md`) 한쪽만 계약을 받으면 같은
  // 프로젝트에서 host에 따라 다른 규칙이 로드된다.
  //
  // 차이는 단 한 곳, `CLAUDE.md`의 `@AGENTS.md` import다. 블록 **밖**은 프로젝트 소유
  // 산문이고 Claude CLI는 루트 `CLAUDE.md`만 자동 로드하므로, 그 줄이 없으면 프로젝트가
  // 블록 밖에 적은 규칙이 Claude와 Claude reviewer에게만 조용히 사라진다. `AGENTS.md`
  // 쪽에는 없어야 한다 — 자기 자신을 import하는 줄이다.
  //
  // 그리고 skill 인덱스 자리: install이 skill 링크를 다 만든 **뒤에** 그 자리만 실제
  // 목록으로 바꾼다. 그래서 마커 바깥 조각만 원문과 대조한다.
  for (const rootLabel of ["AGENTS.md", "CLAUDE.md"]) {
    const rootPath = path.join(tempRoot, rootLabel);
    if (!fs.existsSync(rootPath)) {
      failures.push(`${label} install missing root ${rootLabel}`);
      continue;
    }
    const rootText = fs.readFileSync(rootPath, "utf8");
    const [head, tail] = blockSegmentsOutsideSkillIndex(template);
    const expected = rootLabel === "CLAUDE.md"
      // CLAUDE.md는 end 마커 직전에 import가 들어가 tail이 한 조각으로 남지 않는다.
      // 템플릿 tail에 있는 것은 두 마커뿐이므로 조각으로 나눠 봐도 덮는 범위는 같다.
      ? [head, "<!-- agent-flow:skills:end -->", "@AGENTS.md", "<!-- agent-flow:end -->"]
      : [head, tail];
    for (const segment of expected) {
      if (!rootText.includes(segment)) {
        failures.push(`${label} root ${rootLabel} does not carry bootstrap/AGENTS.md.template verbatim`);
        break;
      }
    }
    if (rootLabel === "AGENTS.md" && rootText.includes("@AGENTS.md")) {
      failures.push(`${label} root AGENTS.md must not import itself`);
    }
  }
}

function blockSegmentsOutsideSkillIndex(text) {
  const start = text.indexOf("<!-- agent-flow:skills:start -->");
  const end = text.indexOf("<!-- agent-flow:skills:end -->");
  if (start === -1 || end === -1) {
    return [text.trimEnd()];
  }
  return [text.slice(0, start).trimEnd(), text.slice(end).trimEnd()];
}

// 두 installer는 같은 파일명과 같은 문장으로 알려야 한다. 문구가 갈라지면
// 사용자는 어느 CLI를 썼는지에 따라 다른 손실 통지를 받는다.
function assertInstallerWorkflowBackupAndTimestamps(label, tempRoot, installer) {
  const workflowsDir = path.join(tempRoot, ".agent-flow", "workflows");
  const kitPath = path.join(tempRoot, ".agent-flow", "kit.json");
  const customRel = path.join(".agent-flow", "workflows", "parity-custom.yaml");
  const custom = path.join(tempRoot, customRel);
  const backup = `${custom}.removed`;
  const customText = "id: parity-custom\nphases: []\n";
  const expectedNotice = `  - pruned: ${customRel} (backup: ${customRel}.removed)`;
  const before = readJsonSafe(kitPath);
  if (typeof before?.installed_at !== "string" || typeof before?.updated_at !== "string") {
    failures.push(`${label} kit.json is missing the installed_at/updated_at pair`);
    return;
  }
  let previous = before;
  for (const args of [["install"], ["install"], ["install", "--force-managed"]]) {
    fs.writeFileSync(custom, customText, "utf8");
    const result = spawnSync(process.execPath, [path.join(SOURCE_ROOT, "bin", installer), ...args], {
      cwd: tempRoot,
      encoding: "utf8",
      env: { ...process.env, AGENT_FLOW_SKIP_CODEX_TRUST: "1" },
      stdio: ["ignore", "pipe", "pipe"],
      timeout: 30_000,
    });
    if (result.error || result.status !== 0) {
      failures.push(`${label} ${args.join(" ")} failed: ${result.error?.message || result.stderr.trim() || result.status}`);
      return;
    }
    if (!result.stdout.split("\n").includes(expectedNotice)) {
      failures.push(`${label} ${args.join(" ")} did not report the pruned workflow as \`${expectedNotice}\``);
    }
    if (fs.existsSync(custom)) {
      failures.push(`${label} ${args.join(" ")} left the extraneous ${customRel}`);
    }
    if (!fs.existsSync(backup) || fs.readFileSync(backup, "utf8") !== customText) {
      failures.push(`${label} ${args.join(" ")} did not back up ${customRel}`);
    }
    const after = readJsonSafe(kitPath);
    if (after?.installed_at !== before.installed_at) {
      failures.push(`${label} ${args.join(" ")} reset kit.json installed_at`);
    }
    if (!(after?.updated_at > previous.updated_at)) {
      failures.push(`${label} ${args.join(" ")} did not refresh kit.json updated_at`);
    }
    previous = after ?? previous;
  }
  const backups = fs.readdirSync(workflowsDir).filter((name) => name.startsWith("parity-custom.yaml.removed"));
  if (backups.length !== 1) {
    failures.push(`${label} repeated install multiplied workflow backups: ${backups.join(", ")}`);
  }
  const legacy = "2020-01-01T00:00:00.000Z";
  const legacyPayload = { ...previous, installed_at: legacy };
  delete legacyPayload.updated_at;
  fs.writeFileSync(kitPath, `${JSON.stringify(legacyPayload, null, 2)}\n`, "utf8");
  const migration = spawnSync(process.execPath, [path.join(SOURCE_ROOT, "bin", installer), "install"], {
    cwd: tempRoot,
    encoding: "utf8",
    env: { ...process.env, AGENT_FLOW_SKIP_CODEX_TRUST: "1" },
    stdio: ["ignore", "pipe", "pipe"],
    timeout: 30_000,
  });
  if (migration.error || migration.status !== 0) {
    failures.push(`${label} legacy kit.json install failed: ${migration.error?.message || migration.stderr.trim() || migration.status}`);
    return;
  }
  const migrated = readJsonSafe(kitPath);
  if (migrated?.installed_at !== legacy) {
    failures.push(`${label} install overwrote the legacy kit.json installed_at`);
  }
  if (!(migrated?.updated_at > legacy)) {
    failures.push(`${label} install did not add updated_at to the legacy kit.json`);
  }
}

function readJsonSafe(pathName) {
  try {
    return JSON.parse(fs.readFileSync(pathName, "utf8"));
  } catch {
    return null;
  }
}

// 배치 계약이 이름 목록을 전부 담고 있는지 세려면 이름 목록도 Python에서 읽어야 한다.
function pythonManagedHookScripts() {
  const source = fs.readFileSync(path.join(SOURCE_ROOT, "src", "agent_flow", "core", "hook_integrity.py"), "utf8");
  const block = source.match(/MANAGED_HOOK_SCRIPTS = \(([\s\S]*?)\)/);
  if (!block) {
    throw new Error("MANAGED_HOOK_SCRIPTS not found in hook_integrity.py");
  }
  return [...block[1].matchAll(/"([^"]+)"/g)].map((match) => match[1]);
}

// Python이 무결성 검증에 쓰는 배치 계약을 그대로 읽는다. JS에 사본을 두면
// 두 목록이 갈라지고, 갈라진 순간 parity가 지키는 대상이 사라진다.
function pythonManagedHookPlacement() {
  const source = fs.readFileSync(path.join(SOURCE_ROOT, "src", "agent_flow", "core", "hook_integrity.py"), "utf8");
  const block = source.match(/MANAGED_HOOK_PLACEMENT = \{([\s\S]*?)\n\}/);
  if (!block) {
    throw new Error("MANAGED_HOOK_PLACEMENT not found in hook_integrity.py");
  }
  const placement = {};
  // 값이 여러 줄 문자열로 쪼개져 있어도 읽는다. Python은 암시적 연결을 하나의
  // 문자열로 보는데, 리터럴 하나만 집으면 계약이 조용히 반쪽으로 읽힌다.
  // 마지막 항목은 블록 캡처가 끝나는 자리라 뒤에 개행이 없다.
  const entry = /"([^"]+)":\s*\(([\s\S]*?)\),(?:\n|$)/g;
  for (const match of block[1].matchAll(entry)) {
    const literals = [...match[2].matchAll(/"((?:[^"\\]|\\.)*)"/g)].map((item) => item[1]);
    if (literals.length === 0) {
      continue;
    }
    const [event, ...rest] = literals;
    placement[match[1]] = [event, rest.join("").replaceAll("\\\\", "\\")];
  }
  // 개수를 세지 않으면 정규식이 못 읽는 형태(암시적 문자열 연결 등)로 적힌 항목이
  // 무음으로 빠지고, 그 항목의 JS/Python 갈라짐이 이 검사를 통과한다. 실측으로
  // `record-skill-read.py`가 그렇게 빠져 있었다.
  const managed = pythonManagedHookScripts();
  const missing = managed.filter((name) => !(name in placement));
  if (missing.length > 0) {
    throw new Error(`MANAGED_HOOK_PLACEMENT entries unreadable: ${missing.join(", ")}`);
  }
  return placement;
}

function assertInstalledHookParity(label, tempRoot) {
  const claude = readJsonSafe(path.join(tempRoot, ".claude", "settings.json"));
  const codex = readJsonSafe(path.join(tempRoot, ".Codex", "hooks.json"));
  const lowerCodex = readJsonSafe(path.join(tempRoot, ".codex", "hooks.json"));
  const ompExtension = path.join(tempRoot, ".omp", "extensions", "agent-flow-hooks.ts");
  if (!claude?.hooks || !codex?.hooks || !lowerCodex?.hooks || !fs.existsSync(ompExtension)) {
    failures.push(`${label} install missing claude, codex, or omp hook settings`);
    return;
  }
  // 이름 목록을 여기서 따로 들면 Python 계약과 갈라진다. 실제 등록을
  // `MANAGED_HOOK_PLACEMENT`와 직접 대조한다 — 이벤트/matcher까지 포함해서.
  // 이 대조가 Python의 hook_integrity가 믿는 계약을 실제 installer 출력에 묶는다.
  const placement = pythonManagedHookPlacement();
  const managedScripts = Object.keys(placement);
  for (const [host, settings] of [["claude", claude], ["codex", codex], ["codex-lower", lowerCodex]]) {
    for (const [script, [event, matcher]] of Object.entries(placement)) {
      const found = [];
      for (const [registeredEvent, blocks] of Object.entries(settings.hooks ?? {})) {
        for (const block of Array.isArray(blocks) ? blocks : []) {
          const commands = (block.hooks ?? []).map((hook) => String(hook.command ?? ""));
          if (commands.some((command) => command.includes(`/hooks/${script}`))) {
            found.push([registeredEvent, String(block.matcher ?? "")]);
          }
        }
      }
      if (found.length === 0) {
        failures.push(`${label} ${host} hooks missing ${script}`);
        continue;
      }
      if (!found.some(([ev, mt]) => ev === event && mt === matcher)) {
        const actual = found.map(([ev, mt]) => `${ev}/${mt || "(none)"}`).join(", ");
        failures.push(
          `${label} ${host} registers ${script} at ${actual}, python contract says ${event}/${matcher || "(none)"}`,
        );
      }
    }
    const stopEntries = settings.hooks.Stop;
    if (Array.isArray(stopEntries) && stopEntries.length !== 1) {
      failures.push(`${label} ${host} Stop hook entries must stay 1 after reinstall, got ${stopEntries.length}`);
    }
  }
  const ompExtensionText = fs.readFileSync(ompExtension, "utf8");
  for (const script of managedScripts) {
    if (!ompExtensionText.includes(script)) {
      failures.push(`${label} omp extension missing ${script}`);
    }
  }
  if (!ompExtensionText.includes("tool_call") || !ompExtensionText.includes("tool_result") || !ompExtensionText.includes("session_shutdown")) {
    failures.push(`${label} omp extension missing tool/session hook events`);
  }
  if (!ompExtensionText.includes("pi.on(\"context\"") || !ompExtensionText.includes('message?.customType === "agent-flow-model-context"') || !ompExtensionText.includes('message?.details?.source === "agent-flow-omp-model-context"') || !ompExtensionText.includes('message?.role === "user"') || !ompExtensionText.includes('text.startsWith("<context>")') || !ompExtensionText.includes('/<file\\b[^>]*\\bsource="agent-flow-omp-model-context"/.test(text)')) {
    failures.push(`${label} omp extension must scrub stale hidden root context messages`);
  }
  if (ompExtensionText.includes("modelSpecificProjectContext") || ompExtensionText.includes("contextMessage(") || ompExtensionText.includes("content.trimEnd()")) {
    failures.push(`${label} omp extension must not inject root context message content`);
  }
  if (ompExtensionText.includes('role: "user"')) {
    failures.push(`${label} omp extension must not inject model context as a visible user message`);
  }
  // 예전에는 이 자리가 "루트 AGENTS.md ↔ CLAUDE.md 전체 파일 미러가 있다"를 못 박았다.
  // 지금은 그 반대를 못 박는다: 루트 두 파일의 agent-flow 블록은 install이 같은 템플릿
  // 한 벌로 관리하고, 블록 **밖**은 프로젝트 소유 산문이다. 전체 파일 미러는 그 산문까지
  // 한쪽으로 덮어써서 install이 보존하는 경계를 깬다. OMP는 루트 `CLAUDE.md`를 읽지도
  // 않는다(`claude` provider는 `.claude/CLAUDE.md`만 본다), 그래서 미러는 얻는 것이 없다.
  // 이 단언은 문자열 `CLAUDE.md` 자체를 금지한다. 그래서 "왜 미러가 없는지"를 생성되는
  // extension **안**의 주석으로는 적을 수 없다 — 그 주석이 곧 위반이 된다. 그 설명이
  // 여기에만 있는 이유이고, 이 제약을 모르면 extension 쪽에 주석을 넣었다가 이 검사가
  // 실패하는 것을 오탐으로 오해하게 된다.
  if (ompExtensionText.includes("syncRootContextFiles") || ompExtensionText.includes("CLAUDE.md")) {
    failures.push(`${label} omp extension must not mirror root AGENTS.md into CLAUDE.md`);
  }
  assertInstalledLauncherParity(label, tempRoot);
}

// launcher는 chat 승인 hook이 CLI를 태우는 유일한 경로다. 이 하네스가 잡는 것은
// **최종 설치 상태**다 — 호출을 `if (false)`로 감싸거나 심는 자리를 없애면 잡힌다.
// 심는 순서(digest보다 나중)는 여기서 안 보인다. 그건 tests/test_cli.py의
// `test_node_installers_write_the_spec_approval_launcher`가 digest 일치로 잡는다.
function assertInstalledLauncherParity(label, tempRoot) {
  const launcher = path.join(tempRoot, pythonLauncherRelative());
  const identity = fs.lstatSync(launcher, { throwIfNoEntry: false });
  if (!identity || !identity.isFile()) {
    failures.push(`${label} install missing the managed launcher: ${pythonLauncherRelative()}`);
    return;
  }
  if (!(identity.mode & 0o100)) {
    failures.push(`${label} managed launcher is not executable`);
  }
  if (identity.mode & 0o022) {
    failures.push(`${label} managed launcher is group or world writable`);
  }
  const kit = readJsonSafe(path.join(tempRoot, ".agent-flow", "kit.json"));
  const digest = createHash("sha256").update(fs.readFileSync(launcher)).digest("hex");
  if (kit?.[pythonLauncherDigestKey()] !== digest) {
    failures.push(
      `${label} kit.json ${pythonLauncherDigestKey()} does not match the installed launcher`,
    );
  }
  const runtimeCli = path.join(tempRoot, pythonRuntimeCliRelative());
  if (!fs.existsSync(runtimeCli)) {
    failures.push(`${label} install missing the runtime the launcher execs: ${pythonRuntimeCliRelative()}`);
  }
}

// launcher 경로와 digest 키는 JS(installer-shared)·Python(hook_integrity)·hook
// 스크립트 세 곳이 각자 선언한다. 언어 경계라 합칠 수 없으므로 같은 값인지 본다 —
// 한쪽만 바뀌면 모든 run이 시작 거부되거나(게이트) 승인이 조용히 죽는다(hook).
function pythonLauncherRelative() {
  const source = fs.readFileSync(
    path.join(SOURCE_ROOT, "src", "agent_flow", "core", "hook_integrity.py"),
    "utf8",
  );
  const block = source.match(/PROJECT_LAUNCHER_RELATIVE = ([^\n]+)/);
  if (!block) {
    throw new Error("PROJECT_LAUNCHER_RELATIVE not found in hook_integrity.py");
  }
  return [...block[1].matchAll(/"([^"]+)"/g)].map((match) => match[1]).join(path.sep);
}

function pythonRuntimeCliRelative() {
  const source = fs.readFileSync(
    path.join(SOURCE_ROOT, "src", "agent_flow", "core", "hook_integrity.py"),
    "utf8",
  );
  const block = source.match(/RUNTIME_CLI_RELATIVE = ([^\n]+)/);
  if (!block) {
    throw new Error("RUNTIME_CLI_RELATIVE not found in hook_integrity.py");
  }
  return [...block[1].matchAll(/"([^"]+)"/g)].map((match) => match[1]).join(path.sep);
}

function pythonLauncherDigestKey() {
  const source = fs.readFileSync(
    path.join(SOURCE_ROOT, "src", "agent_flow", "core", "hook_integrity.py"),
    "utf8",
  );
  // 접근 형태(`kit.get(...)` / `kit[...]` / `... in kit`)는 바뀔 수 있으니 키 이름만 본다.
  const match = source.match(/"(project_launcher_digest)"/);
  if (!match) {
    throw new Error("launcher digest key not found in hook_integrity.py");
  }
  return match[1];
}

function assertLauncherContractIsSingleValued() {
  const jsRelative = PROJECT_LAUNCHER_RELATIVE;
  if (jsRelative !== pythonLauncherRelative()) {
    failures.push(
      `launcher path differs: installer-shared has ${jsRelative}, hook_integrity has ${pythonLauncherRelative()}`,
    );
  }
  for (const installer of ["bin/agent-flow-kit.mjs", "bin/agent-flow-install.mjs"]) {
    assertContains(installer, `${pythonLauncherDigestKey()}: projectLauncherDigest(`);
  }
}

// AGENTS.md에 심은 인덱스가 실제 설치본과 같은가.
//
// 인덱스는 agent가 "이 프로젝트에 뭐가 있나"를 판단 없이 아는 유일한 경로다.
// 낡은 인덱스는 없는 것보다 나쁘다 - 없는 skill을 찾게 만들고, 있는 skill을
// 숨긴다. 그래서 목록 자체를 설치 결과와 대조한다.
function assertSkillIndexBlockMatchesInstall(label, tempRoot) {
  const index = readJsonSafe(path.join(tempRoot, ".agent-flow", "skills", "index.json"));
  if (!index || !Array.isArray(index.skills)) {
    return;
  }
  const expected = new Set(index.skills.map((skill) => String(skill.name)));
  const expectedPassive = new Set(
    index.skills.filter((skill) => skill.delivery === "passive").map((skill) => String(skill.name)),
  );
  // 인덱스는 루트 두 파일 모두에 심는다. host마다 자동 로드하는 파일이 달라서
  // (Claude는 `CLAUDE.md`, Codex/OMP는 `AGENTS.md`) 한 곳만 채우면 나머지 host는
  // 설치된 skill 목록을 못 본다. 두 곳을 같은 생성기가 쓰므로 갈라질 자리는 없다.
  for (const fileName of ["AGENTS.md", "CLAUDE.md"]) {
    const target = path.join(tempRoot, fileName);
    if (!fs.existsSync(target)) {
      failures.push(`${label} ${fileName} missing after install`);
      continue;
    }
    const text = fs.readFileSync(target, "utf8");
    const start = text.indexOf("<!-- agent-flow:skills:start -->");
    const end = text.indexOf("<!-- agent-flow:skills:end -->");
    if (start === -1 || end === -1) {
      failures.push(`${label} ${fileName} has no skill index block`);
      continue;
    }
    const block = text.slice(start, end);
    if (!block.includes("[agent-flow skill index]")) {
      failures.push(`${label} ${fileName} skill index block was never filled`);
      continue;
    }
    // 두 그룹 모두 이름만 있는 한 줄이다: `|always:{...}` / `|on-demand:{...}`.
    const listed = new Set(
      [...block.matchAll(/\|(?:always|on-demand):\{([^}]*)\}/g)]
        .flatMap((match) => match[1].split(","))
        .map((name) => name.trim())
        .filter(Boolean),
    );
    for (const name of expected) {
      if (!listed.has(name)) {
        failures.push(`${label} ${fileName} skill index omits installed skill: ${name}`);
      }
    }
    for (const name of listed) {
      if (!expected.has(name)) {
        failures.push(`${label} ${fileName} skill index lists a skill that is not installed: ${name}`);
      }
    }
    const alwaysMatch = block.match(/\|always:\{([^}]*)\}/);
    const always = new Set(
      (alwaysMatch ? alwaysMatch[1].split(",") : []).map((name) => name.trim()).filter(Boolean),
    );
    for (const name of expectedPassive) {
      if (!always.has(name)) {
        failures.push(`${label} ${fileName} skill index does not mark ${name} as always-applied`);
      }
    }
    for (const name of always) {
      if (!expectedPassive.has(name)) {
        failures.push(`${label} ${fileName} skill index claims ${name} is always-applied, frontmatter says otherwise`);
      }
    }
  }
}

function assertSkillIndexComplete(label, tempRoot) {
  const skillsDir = path.join(tempRoot, ".agent-flow", "skills");
  const index = readJsonSafe(path.join(skillsDir, "index.json"));
  if (!index || !Array.isArray(index.skills)) {
    failures.push(`${label} skill index unreadable`);
    return;
  }
  const indexed = new Set(index.skills.map((skill) => skill.name));
  for (const entry of fs.readdirSync(skillsDir, { withFileTypes: true })) {
    if (!entry.isDirectory()) {
      continue;
    }
    if (!fs.existsSync(path.join(skillsDir, entry.name, "SKILL.md"))) {
      continue;
    }
    if (!indexed.has(entry.name)) {
      failures.push(`${label} installed skill missing from index.json: ${entry.name}`);
    }
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
  for (const host of ["claude", "codex", "omp"]) {
    const skillDir = path.join(hostSkillRoot(root, host), "agent-flow");
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
  for (const host of ["claude", "codex", "omp"]) {
    const skillDir = path.join(hostSkillRoot(root, host), staleName);
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
      hosts: ["claude", "codex", "omp"],
      priority: 50,
      hash: staleHash,
      warnings: [],
    },
  ];
  index.links = [
    ...(index.links ?? []),
    { name: staleName, host: "claude", path: ".claude/skills/demo-stale", status: "copied" },
    { name: staleName, host: "codex", path: ".Codex/skills/demo-stale", status: "copied" },
    { name: staleName, host: "omp", path: ".omp/skills/demo-stale", status: "copied" },
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

function hostSkillRoot(root, host) {
  if (host === "codex") {
    return path.join(root, ".Codex", "skills");
  }
  if (host === "omp") {
    return path.join(root, ".omp", "skills");
  }
  return path.join(root, `.${host}`, "skills");
}

function hostSkillFile(root, host, name) {
  return path.join(hostSkillRoot(root, host), name, "SKILL.md");
}

function assertHostSkillParity(root, index, label = "clean install") {
  const expectedHosts = ["claude", "codex", "omp"];
  const skills = new Map((index.skills ?? []).map((skill) => [skill.name, skill]));
  const links = index.links ?? [];
  for (const [name, skill] of skills) {
    const hosts = new Set(skill.hosts ?? []);
    if (!expectedHosts.every((host) => hosts.has(host))) {
      continue;
    }
    const hostSkillFiles = expectedHosts.map((host) => [host, hostSkillFile(root, host, name)]);
    if (skill.source === "bundled" && !BUNDLED_HOST_SKILL_NAMES.has(name)) {
      // 설치 계약: allowlist 밖 bundled skill은 host link를 만들지 않는다.
      for (const [host, skillFile] of hostSkillFiles) {
        if (fs.existsSync(skillFile)) {
          failures.push(`${label} unexpected ${host} skill link for bundled ${name}`);
        }
      }
      continue;
    }
    const sourceSkill = path.join(root, skill.path);
    if (!fs.existsSync(sourceSkill)) {
      failures.push(`${label} missing project skill source for ${name}`);
      continue;
    }
    for (const [host, skillFile] of hostSkillFiles) {
      if (!fs.existsSync(skillFile)) {
        failures.push(`${label} missing ${host} skill link for ${name}`);
        continue;
      }
      if (fs.readFileSync(skillFile, "utf8") !== fs.readFileSync(sourceSkill, "utf8")) {
        failures.push(`${label} ${host}/project skill content differs for ${name}`);
      }
    }
    const hostLinks = expectedHosts.map((host) => links.find((link) => link.name === name && link.host === host));
    if (hostLinks.some((link) => !link)) {
      failures.push(`${label} missing host index link set for ${name}`);
      continue;
    }
    const statuses = new Set(hostLinks.map((link) => link.status));
    if (statuses.size !== 1) {
      failures.push(`${label} host link status differs for ${name}: ${hostLinks.map((link) => `${link.host}=${link.status}`).join(" ")}`);
    }
  }
}

function preferredPython() {
  const virtualEnvPython = process.env.VIRTUAL_ENV
    ? path.join(process.env.VIRTUAL_ENV, process.platform === "win32" ? "Scripts/python.exe" : "bin/python")
    : null;
  // HOME이 바뀌면 user-site의 yaml을 잃는 시스템 python 대신 kit 자체 venv를 우선한다.
  const kitVenvPython = path.join(SOURCE_ROOT, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  const candidates = [
    process.env.PYTHON,
    process.env.PYTHON_EXECUTABLE,
    virtualEnvPython,
    fs.existsSync(kitVenvPython) ? kitVenvPython : null,
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
