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
// 두 runner에 **같은** nonce를 준다. node는 `run start`가 무작위로 심고 python은
// meta에서 읽으므로, 고정하지 않으면 같은 입력이 아니게 되어 provenance 검사가
// parity 오탐으로 보인다.
const PARITY_GATE_NONCE = "parity-gate-nonce";

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

for (const installer of ["bin/agent-flow-kit.mjs", "bin/agent-flow-install.mjs"]) {
  assertContains(installer, "function installCodexTrustState(root)");
  assertContains(installer, "function installOmpHooks(root)");
  assertContains(installer, ".omp\", \"extensions\", \"agent-flow-hooks.ts");
  // 두 installer가 같은 인덱스를 만들어야 한다. 한쪽만 채우면 install 순서에
  // 따라 AGENTS.md의 인덱스가 있었다 없었다 한다.
  assertContains(installer, "function skillIndexBlock(root)");
  assertContains(installer, "function upsertSkillIndexBlock(root)");
  assertContains(installer, 'const SKILL_INDEX_START = "<!-- agent-flow:skills:start -->"');
  // install이 **현재 등록된** hook의 해시를 trusted로 되받아 적으면, 변조된
  // 등록이 다음 install에서 승인 상태로 세탁된다. 읽는 코드도 없었다.
  // 등록 무결성은 런 시작 시 hook_integrity가 kit.json과 대조한다.
  assertAbsent(installer, "[hooks.state.", "install must not launder managed hook approval");
  assertAbsent(installer, "trusted_hash", "install must not launder managed hook approval");
}

// 관리 hook 이름은 이제 등록 지점이 4곳이다 — installer 2개, 이 파일, 그리고
// 런 시작 무결성 검증(Python). 갈라지면 검증이 조용히 좁아진다.
{
  const jsManagedScripts = (() => {
    const text = readIfExists("bin/agent-flow-kit.mjs");
    if (text === null) return null;
    const match = text.match(/const MANAGED_HOOK_SCRIPTS = \[([\s\S]*?)\];/);
    if (!match) {
      failures.push("bin/agent-flow-kit.mjs missing MANAGED_HOOK_SCRIPTS");
      return null;
    }
    return [...match[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]).sort();
  })();
  if (jsManagedScripts) {
    assertPythonContract("managed hook script parity", `
from agent_flow.core.hook_integrity import MANAGED_HOOK_SCRIPTS
expected = ${JSON.stringify(jsManagedScripts)}
assert sorted(MANAGED_HOOK_SCRIPTS) == expected, (sorted(MANAGED_HOOK_SCRIPTS), expected)
`);
  }
}

// 두 installer가 심는 OMP 확장은 **바이트 단위로** 같아야 한다. 예전에 한쪽만
// `tool_result` 핸들러 등록 순서가 달라서, 이벤트당 핸들러 하나만 남기는 host에서
// 루트 컨텍스트 동기화가 통째로 죽었다. 규율이 아니라 검사로 묶는다.
{
  const sources = ["bin/agent-flow-kit.mjs", "bin/agent-flow-install.mjs"].map((rel) => {
    const text = readIfExists(rel);
    if (text === null) return null;
    const start = text.indexOf("function ompHooksExtensionSource() {");
    const end = text.indexOf("\n`;\n}", start);
    if (start < 0 || end < 0) {
      failures.push(`${rel} missing ompHooksExtensionSource() body`);
      return null;
    }
    return text.slice(start, end);
  });
  if (sources[0] !== null && sources[1] !== null && sources[0] !== sources[1]) {
    failures.push("omp extension source diverged between bin/agent-flow-kit.mjs and bin/agent-flow-install.mjs");
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
assertContains("bin/agent-flow-kit.mjs", "RUNTIME_PYTHON_RELATIVE");
assertContains("bin/agent-flow-kit.mjs", "src\", \"agent_flow");
assertContains("src/agent_flow/core/gates.py", "\".agent-flow\" / \"runtime\" / \"python\"");
assertContains("src/agent_flow/core/gates.py", "_resolve_gate_command");
assertContains("scripts/check-context-docs.mjs", 'path.basename(SCRIPT_ROOT) === ".agent-flow"');
assertPythonContract("profile gate build/typecheck/lint order", `
from agent_flow.cli import _profile_gate_commands

def gate_ids(profile):
    return [command.gate_id for command in _profile_gate_commands([profile])]

def require_before(ids, left, right):
    if ids.index(left) > ids.index(right):
        raise AssertionError(f"{left} must run before {right}: {ids}")

typescript = gate_ids("typescript")
for profile in ("android", "generic", "ios", "nextjs", "node", "python", "react-native", "spring", "typescript"):
    ids = gate_ids(profile)
    if "architecture-lint" not in ids:
        raise AssertionError(f"{profile} missing architecture-lint gate: {ids}")
generic_commands = [command.command for command in _profile_gate_commands(["generic"])]
if ("node", "scripts/check-context-docs.mjs") not in generic_commands:
    raise AssertionError(generic_commands)
require_before(typescript, "build", "typecheck")
require_before(typescript, "typecheck", "lint")

react_native = gate_ids("react-native")
require_before(react_native, "android-build", "lint")
require_before(react_native, "ios-build", "lint")

union = [command.gate_id for command in _profile_gate_commands(["android", "react-native"])]
union_commands = [command.command for command in _profile_gate_commands(["android", "react-native"])]
require_before(union, "react-native:android-build", "architecture-lint")
require_before(union, "react-native:android-build", "android:lint")
architecture_command = next((command for command in union_commands if command[-2:] == ("--profile", "android,react-native")), None)
if architecture_command is None or "agent_flow.core.architecture_lint" not in architecture_command:
    raise AssertionError(union_commands)
if any(command[-2:] == ("--profile", "android") for command in union_commands):
    raise AssertionError(union_commands)
	`);
assertPythonContract("react-native profile wins over Gradle", `
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from agent_flow.core.profiles import detect_profile

with TemporaryDirectory() as temp_dir:
    root = Path(temp_dir)
    (root / "package.json").write_text(json.dumps({"dependencies": {"react-native": "latest"}}), encoding="utf-8")
    (root / "settings.gradle.kts").write_text("", encoding="utf-8")
    if detect_profile(root) != "react-native":
        raise AssertionError(detect_profile(root))
`);
assertPythonContract("multi-profile architecture lint partitions files", `
from pathlib import Path
from tempfile import TemporaryDirectory
from agent_flow.core.architecture_lint import lint_profiles

with TemporaryDirectory() as temp_dir:
    root = Path(temp_dir)
    source = root / "core" / "domain" / "chat" / "src" / "main" / "java" / "com" / "example" / "app" / "core" / "domain" / "chat" / "Chat.kt"
    source.parent.mkdir(parents=True)
    source.write_text("package com.example.app.core.domain.chat\\nclass Chat\\n", encoding="utf-8")
    (root / "core" / "data" / "chat").mkdir(parents=True)
    findings = lint_profiles(root, ["android", "react-native"], files=[str(source.relative_to(root))])
    if findings["android"] or findings["react-native"]:
        raise AssertionError(findings)

    outside = root / "components" / "Button.tsx"
    outside.parent.mkdir(parents=True)
    outside.write_text("export function Button() { return null }\\n", encoding="utf-8")
    outside_findings = lint_profiles(root, ["nextjs", "android"], files=[str(outside.relative_to(root))])
    if not any(item.message == "path is outside profile architecture role mapping" for values in outside_findings.values() for item in values):
        raise AssertionError(outside_findings)

    rn_android = root / "android" / "app" / "src" / "main" / "java" / "com" / "example" / "MainApplication.kt"
    rn_android.parent.mkdir(parents=True)
    rn_android.write_text("package com.example\\nclass MainApplication\\n", encoding="utf-8")
    rn_findings = lint_profiles(root, ["react-native"], files=[str(rn_android.relative_to(root))])
    if rn_findings["react-native"] or rn_findings["android"]:
        raise AssertionError(rn_findings)

    bad_android = root / "android" / "app" / "src" / "main" / "java" / "com" / "example" / "CheckoutDTO.kt"
    bad_android.write_text("package com.example\\nclass CheckoutDTO\\n", encoding="utf-8")
    bad_findings = lint_profiles(root, ["react-native"], files=[str(bad_android.relative_to(root))])
    if not any("forbidden token Dto" in item.message for item in bad_findings["android"]):
        raise AssertionError(bad_findings)
`);
assertPythonContract("architecture lint parses Gradle type-safe accessors", `
from pathlib import Path
from tempfile import TemporaryDirectory
from agent_flow.core.architecture_lint import lint_project

with TemporaryDirectory() as temp_dir:
    root = Path(temp_dir)
    source = root / "core" / "domain" / "chat" / "src" / "main" / "java" / "com" / "example" / "app" / "core" / "domain" / "chat" / "Chat.kt"
    source.parent.mkdir(parents=True)
    source.write_text("package com.example.app.core.domain.chat\\nclass Chat\\n", encoding="utf-8")
    (root / "core" / "data" / "chat").mkdir(parents=True)
    build = root / "core" / "domain" / "chat" / "build.gradle.kts"
    build.write_text("dependencies { implementation(projects.core.data.chat) }\\n", encoding="utf-8")
    findings = lint_project(root, "android", files=[str(source.relative_to(root))])
    if not any(":core:data" in item.message for item in findings):
        raise AssertionError(findings)
`);
assertPythonContract("gate results use workflow artifact schema", `
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from agent_flow.core.artifacts import write_gate_results
from agent_flow.core.gates import GateResult

with TemporaryDirectory() as temp_dir:
    run_dir = Path(temp_dir)
    path = write_gate_results(run_dir=run_dir, results=[
        GateResult("build", ("npm", "run", "build"), True, 0, "ok", "")
    ])
    if path != run_dir / "artifacts" / "gate-results.json":
        raise AssertionError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("passed") is not True or not isinstance(payload.get("results"), list):
        raise AssertionError(payload)
    if payload["results"][0].get("command") != "npm run build":
        raise AssertionError(payload)
`);
assertPythonContract("gate status green requires evidence", `
from agent_flow.runner import _gates_route_key

if _gates_route_key('{"passed": true, "status": "green"}') != "default":
    raise AssertionError("status-only gate passed")
`);
assertPythonContract("unknown profile is controlled error", `
from pathlib import Path
from tempfile import TemporaryDirectory
from agent_flow.cli import main

with TemporaryDirectory() as temp_dir:
    code = main(["gates", "--root", temp_dir, "--profile", "does-not-exist"])
    if code != 1:
        raise AssertionError(code)
`);

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
  assertNotContains(rel, "Gemini sub-agent in Gemini");
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
assertNotContains("workflows/default.yaml", "Gemini sub-agent");
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
  assertNotContains(".agent-flow/prompts/multi-review.md", "Gemini sub-agent");
  assertContains(".agent-flow/prompts/multi-review.md", "reviewer-source: sub-agent");
  assertContains(".agent-flow/prompts/multi-review.md", "close that sub-agent session");
  assertContains(".agent-flow/prompts/multi-review.md", "## Overall");
  assertContains(".agent-flow/prompts/multi-review.md", "verdict: approve");
  assertContains(".agent-flow/prompts/multi-review.md", "verdict: request-changes");
  assertNotContains("workflows/full-feature.yaml", "Gemini sub-agent");
  assertNotContains("bootstrap/AGENTS.md.template", "Gemini sub-agent");
  assertNotContains("bootstrap/CLAUDE.md.template", "Gemini sub-agent");
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
if (CHECK_INSTALLED_COPY) {
  assertSameBodyAfterTitle(".agent-flow/bootstrap/AGENTS.md", ".agent-flow/bootstrap/CLAUDE.md");
}
for (const rel of [
  "bootstrap/AGENTS.md.template",
  "bootstrap/CLAUDE.md.template",
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
  assertContains(rel, "현재 사용 중인 CLI(활성 host)의 sub-agent 2개가 필수");
  assertContains(rel, "활성 host가 아닌 추가 provider는 optional");
  assertNotContains(rel, "예: Claude/Gemini");
  assertContains(rel, "reviewer-source: sub-agent");
  assertContains(rel, "sub-agent를 닫는다");
  assertContains(rel, "## Overall");
  assertContains(rel, "verdict: approve");
  assertContains(rel, "verdict: request-changes");
  assertContains(rel, "보호 브랜치 commit/push와 leader checkout/switch 금지는 모든 host에서 동일");
}

assertFile(".Codex/agents/code-reviewer.md");
assertFile(".claude/agents/code-reviewer.md");
assertSameBodyAfterOptionalFrontmatter(".Codex/agents/code-reviewer.md", ".claude/agents/code-reviewer.md");
assertContains(".claude/agents/code-reviewer.md", "name: code-reviewer");
assertContains(".claude/agents/code-reviewer.md", "description:");

if (CHECK_INSTALLED_COPY) {
  assertContains(".agent-flow/rules/workflow-contract.md", "Required review happens before completion QA");
  assertContains(".agent-flow/rules/workflow-contract.md", "gates run BUILD -> TYPECHECK -> LINT");
  assertContains(".agent-flow/rules/workflow-contract.md", "default workflow, gates run as their own phase");
  assertContains(".agent-flow/rules/workflow-contract.md", "short Korean");
  assertContains(".agent-flow/rules/workflow-contract.md", "two active-host sub-agents");
  assertNotContains(".agent-flow/rules/workflow-contract.md", "Gemini sub-agent in Gemini");
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

function assertAllWorkflowContracts() {
  for (const name of yamlFileNames("workflows").map((file) => file.replace(/\.yaml$/, ""))) {
    const workflow = workflowExport(name);
    if (!workflow) {
      continue;
    }
    if (workflow.id !== name) {
      failures.push(`workflow export ${name} id mismatch: ${workflow.id}`);
    }
    assertWorkflowArtifactContract(workflow);
    assertRouteParity(workflow);
    assertFixLoopRoundParity(workflow);
    assertBackwardFreshArtifactSafety(workflow);
    assertCompletionMarkerPrefixParity(workflow);
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

function provenGateResults(body) {
  return JSON.stringify({
    ...body,
    produced_by: { tool: "agent-flow gates", nonce: PARITY_GATE_NONCE },
  }) + "\n";
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
      "gates passed with evidence but no provenance",
      "gates",
      "{\"passed\": true, \"results\": [{\"command\": \"npm test\", \"passed\": true, \"output\": \"ok\"}]}\n",
    ],
    [
      "gates passed with evidence and provenance",
      "gates",
      provenGateResults({
        passed: true,
        results: [{ command: "npm test", passed: true, output: "ok" }],
      }),
    ],
  ];
  for (const phase of workflow.phases) {
    for (const key of Object.keys(phase.routes ?? {})) {
      cases.push([`${phase.id} ${key}`, phase.id, routeArtifactContent(phase, key)]);
    }
  }
  for (const [label, phaseId, content] of cases) {
    if (!phaseIds.has(phaseId)) {
      continue;
    }
    const python = pythonPhaseOutcome(workflow, phaseId, content, { gate_nonce: PARITY_GATE_NONCE });
    const node = nodePhaseOutcome(workflow, phaseId, content, { gate_nonce: PARITY_GATE_NONCE });
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
  // marker만으로 완료가 결정되는 모든 phase를 검사한다. verdict/status/JSON으로
  // 라우팅되는 phase는 marker 외 입력이 outcome을 좌우하므로 제외한다.
  const prefixes = ["- [x] ", "* ", "+ ", "- ", ""];
  for (const phase of workflow.phases) {
    const markers = phase.required_markers ?? [];
    if (markers.length === 0 || phase.multi_review) {
      continue;
    }
    const routeKeys = Object.keys(phase.routes ?? {});
    if (routeKeys.some((key) => key !== "default")) {
      continue;
    }
    const lines = markers.map((marker, index) => `${prefixes[index % prefixes.length]}${renderCompletionMarker(marker)}`);
    const content = ["## Completion Gate", ...lines, ...skillGateLines(phase), ""].join("\n");
    const pythonMissing = pythonMissingCompletionMarkers(content, markers);
    const node = nodePhaseOutcome(workflow, phase.id, content);
    if (pythonMissing === null || !node) {
      continue;
    }
    const pythonPasses = pythonMissing.length === 0;
    const nodePasses = node.outcome !== "blocked";
    if (pythonPasses !== nodePasses) {
      failures.push(
        `python/node completion marker prefix mismatch (${phase.id}): python_missing=${pythonMissing.join(",")} node=${node.outcome}`,
      );
    }
  }
}

function assertFixLoopRoundParity(workflow) {
  const phase = workflow.phases.find((candidate) => Object.values(candidate.routes ?? {}).includes("fix-loop"));
  if (!phase) {
    return;
  }
  const key = Object.entries(phase.routes).find(([, target]) => target === "fix-loop")?.[0];
  const content = routeArtifactContent(phase, key);
  const pythonAllowed = pythonPhaseOutcome(workflow, phase.id, content, { fix_loop_rounds: 2 });
  const nodeAllowed = nodePhaseOutcome(workflow, phase.id, content, { fix_loop_rounds: 2 });
  if (pythonAllowed && nodeAllowed) {
    if (pythonAllowed.outcome !== nodeAllowed.outcome || pythonAllowed.fix_loop_rounds !== nodeAllowed.fix_loop_rounds) {
      failures.push(`python/node fix-loop round 3 mismatch (${workflow.id} ${phase.id})`);
    }
  }
  const pythonBlocked = pythonPhaseOutcome(workflow, phase.id, content, { fix_loop_rounds: 3 });
  const nodeBlocked = nodePhaseOutcome(workflow, phase.id, content, { fix_loop_rounds: 3 });
  if (pythonBlocked && nodeBlocked && (pythonBlocked.outcome !== "blocked" || nodeBlocked.outcome !== "blocked")) {
    failures.push(`python/node fix-loop round cap mismatch (${workflow.id} ${phase.id}): python=${pythonBlocked.outcome} node=${nodeBlocked.outcome}`);
  }
  if (pythonBlocked && nodeBlocked && pythonBlocked.fix_loop_rounds !== nodeBlocked.fix_loop_rounds) {
    failures.push(`python/node blocked fix-loop round state mismatch (${workflow.id} ${phase.id}): python=${pythonBlocked.fix_loop_rounds} node=${nodeBlocked.fix_loop_rounds}`);
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
  if (phase.id === "gates") {
    if (key === "green" || key === "approve") {
      return JSON.stringify({
        passed: true,
        status: key,
        results: [{ command: "npm test", passed: true, output: "ok" }],
      }) + "\n";
    }
    if (key === "request-changes" || key === "blocked" || key === "error" || key === "pending") {
      return JSON.stringify({ passed: false, status: key, results: [] }) + "\n";
    }
    return JSON.stringify({ passed: true }) + "\n";
  }
  if (phase.multi_review) {
    return phaseArtifactWithMarkers(phase, multiReviewArtifactContent(key, phase.id));
  }
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

function multiReviewArtifactContent(key, phaseId = "") {
  const reviewerAVerdict = key === "request-changes" ? "request-changes" : "approve";
  const reviewerBVerdict = "approve";
  const overall = key === "request-changes" ? "request-changes" : "approve";
  return [
    "## Reviewer A",
    "reviewer-source: sub-agent",
    `verdict: ${reviewerAVerdict}`,
    "",
    "## Reviewer B",
    "reviewer-source: sub-agent",
    `verdict: ${reviewerBVerdict}`,
    "",
    "## Overall",
    `verdict: ${overall}`,
    "",
  ].join("\n");
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
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true });
  }
}

function readJsonSafe(pathName) {
  try {
    return JSON.parse(fs.readFileSync(pathName, "utf8"));
  } catch {
    return null;
  }
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
  const entry = /"([^"]+)":\s*\(\s*"([^"]*)",\s*\n?\s*"((?:[^"\\]|\\.)*)",?\s*\)/g;
  for (const match of block[1].matchAll(entry)) {
    placement[match[1]] = [match[2], match[3].replaceAll("\\\\", "\\")];
  }
  if (Object.keys(placement).length === 0) {
    throw new Error("MANAGED_HOOK_PLACEMENT parsed empty");
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
  if (!ompExtensionText.includes("syncRootContextFiles") || !ompExtensionText.includes("modifiedRootContextFiles")) {
    failures.push(`${label} omp extension missing root AGENTS.md/CLAUDE.md sync`);
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

function pythonBackwardFreshArtifactOutcome(workflow, testCase) {
  const code = String.raw`
import json
import sys
import tempfile
from pathlib import Path

from agent_flow.runner import Phase, Runner
from agent_flow.core.skill_resolver import PhaseSkills


def _phase_skills(value):
    if not isinstance(value, dict):
        return None
    required = tuple(value.get("required") or ())
    optional = tuple(value.get("optional") or ())
    return PhaseSkills(required=required, optional=optional) if (required or optional) else None


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
            skills=_phase_skills(item.get("skills")),
        ))
    target_index = next(i for i, phase in enumerate(phases) if phase.id == test_case["target"])
    current_index = next(i for i, phase in enumerate(phases) if phase.id == test_case["source"])
    for phase in phases[target_index:current_index + 1]:
        artifact = run_dir / phase.artifact
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(test_case["content"] if phase.id == test_case["source"] else "stale\n", encoding="utf-8")
    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.config_root = run_dir
    runner.project_root = run_dir
    runner.profile = {}
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
      env: { ...process.env, AGENT_FLOW_SKIP_CODEX_TRUST: "1" },
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
    ...skillGateLines(phase),
    "",
  ].join("\n");
}

// runner가 phase.skills로부터 주입하는 marker는 required_markers에 없다.
// 그 계약도 fixture에 담아야 Python/Node가 같은 조건에서 비교된다.
function skillGateLines(phase) {
  const required = phase.skills?.required ?? [];
  if (required.length === 0) {
    return [];
  }
  return [
    "skill-availability: pass",
    "skill-read-evidence: unavailable",
    "project-local-skills: checked",
    `project-local-skills-used: ${required.join(", ")}`,
    "project-local-skill-docs: applied",
  ];
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

var nodeRouteKeyEvaluator = null;

function nodeRouteKeyFromKit(phase, content, state = {}) {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-route-key-"));
  try {
    const artifact = path.join(tempRoot, "artifact.txt");
    fs.writeFileSync(artifact, content, "utf8");
    if (!nodeRouteKeyEvaluator) {
      nodeRouteKeyEvaluator = buildNodeRouteKeyEvaluator();
    }
    return nodeRouteKeyEvaluator(phase, artifact, state);
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
    "state",
    `${routeKeySource}\nreturn nodeRouteKey(phase, artifact, state);`,
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
from agent_flow.core.skill_resolver import PhaseSkills


def _phase_skills(value):
    if not isinstance(value, dict):
        return None
    required = tuple(value.get("required") or ())
    optional = tuple(value.get("optional") or ())
    return PhaseSkills(required=required, optional=optional) if (required or optional) else None


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
            skills=_phase_skills(item.get("skills")),
        ))
    index = next(i for i, phase in enumerate(phases) if phase.id == phase_id)
    artifact = run_dir / phases[index].artifact
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(content, encoding="utf-8")
    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.config_root = run_dir
    runner.project_root = run_dir
    runner.profile = {}
    runner.phases = phases
    if phases[index].multi_review:
        route_key = _multi_review_route_key(content, phases[index].id)
    elif phases[index].id == "gates":
        route_key = _gates_route_key(content, nonce=str(read_meta(run_dir).get("gate_nonce", "")))
    else:
        route_key = _route_key(content)
    if route_key == "approve" and phases[index].routes and phases[index].routes.get("request-changes") and has_failure_markers(content):
        route_key = "request-changes"
    try:
        # Node의 advance는 completion marker를 먼저 강제한다. 같은 계층을 비교해야 parity가 의미를 갖는다.
        if runner._missing_required_markers(phases[index]):
            outcome = "blocked"
        else:
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
      env: { ...process.env, AGENT_FLOW_SKIP_CODEX_TRUST: "1" },
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
    const routeKey = nodeRouteKeyFromKit(phase, content, state);
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
