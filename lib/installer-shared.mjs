// 두 install 진입점이 공유하는 순수 헬퍼. 예전에는 같은 본문이 양쪽에 한 벌씩
// 있었고, 그래서 둘이 갈라지지 않았는지 확인하는 검사가 따로 필요했다.
//
// 여기 있는 것은 모듈 상태에 기대지 않는 함수들뿐이다. `installCodexHooks`처럼
// 각 진입점의 전역(`PROJECT`, `AF_DIR`)을 읽는 본문은 아직 각자 갖고 있다.
//
// `hooksDisabled`는 진입점마다 다른 시점에 정해지므로 인자로 받는다. 기본값을 두면
// 인자를 빠뜨린 호출이 hook을 켜 둔 것으로 조용히 처리된다.

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { MANAGED_HOOK_SCRIPTS, RETIRED_MANAGED_HOOK_SCRIPTS } from "./managed-hooks.mjs";
import { OMP_EXTENSION_MARKER } from "./omp-hooks-extension.mjs";
import { SKILL_DEPENDENCIES } from "./skill-selection.mjs";

// prune 알림의 접두사. `agent-flow-install.mjs`가 자식 kit install의 stdout에서
// 이 접두사로 시작하는 줄만 되살리므로, 사용자에게 보여야 하는 prune 알림은
// 반드시 이 접두사를 써야 한다.
export const PRUNE_NOTICE_PREFIX = "  - pruned: ";

// profile YAML은 프로젝트의 stack에 딸린 자산이다. kit이 배포하는 전부를 깔면
// `.agent-flow/profiles/`에 남의 stack 정의가 9개 쌓이고, 사용자는 어느 파일이
// 실제로 읽히는지 구분할 수 없다 — 실제로 android 프로젝트에서 nextjs.yaml을
// 고치는 오류가 났다.
//
// `generic`은 지울 수 없다. runner가 profile을 못 찾을 때 마지막으로 읽는
// fallback 경로다(`runner.py` `_load_single_profile`). `_schema.yaml`은 profile
// 필드의 정의서라서 남긴다.
export const ALWAYS_INSTALLED_PROFILE_FILES = new Set(["_schema.yaml", "generic.yaml"]);

export function installedProfileFileNames(profileIds, profilesDir) {
  const names = new Set(ALWAYS_INSTALLED_PROFILE_FILES);
  const pending = [...profileIds];
  const seen = new Set();
  while (pending.length > 0) {
    const profileId = pending.pop();
    if (typeof profileId !== "string" || !profileId || seen.has(profileId)) {
      continue;
    }
    seen.add(profileId);
    names.add(`${profileId}.yaml`);
    pending.push(...escalatedProfileIds(profilesDir, profileId));
  }
  return names;
}

// `skills.required_review[*].profiles`는 **다른** profile의 skill 표를 끌어온다
// (`profile_routing._table_owner`; react-native의 `android/**` 변경이 Android 표를
// 쓰는 경로다). 끌어오는 profile YAML이 설치본에 없으면 `load_profile_payload`가
// 던지고 `_table_owner`가 그 예외를 삼켜서, 필수 skill이 경고도 missing 보고도
// 없이 사라진다. 그래서 참조 대상도 함께 깐다.
//
// YAML 파서를 쓰지 않는 이유는 이 진입점이 의존성 없는 node 스크립트라는 것이다.
// 대신 flow(`profiles: [a, b]`)와 block(`profiles:` + `- a`) 양쪽을 다 읽는다.
// 한쪽만 읽으면 YAML 스타일만 바꿔도 escalation이 조용히 사라진다 — PyYAML에는
// 두 형태가 같은 값이라 kit 쪽 routing은 계속 통과한다.
function escalatedProfileIds(profilesDir, profileId) {
  const profilePath = path.join(profilesDir, `${profileId}.yaml`);
  if (!fs.existsSync(profilePath)) {
    return [];
  }
  const ids = [];
  let inSkills = false;
  let inRequiredReview = false;
  let blockIndent = null;
  for (const line of fs.readFileSync(profilePath, "utf8").split(/\r?\n/)) {
    if (/^\S/.test(line)) {
      inSkills = line.trim() === "skills:";
      inRequiredReview = false;
      blockIndent = null;
      continue;
    }
    if (!inSkills) {
      continue;
    }
    if (/^  \S/.test(line)) {
      inRequiredReview = /^  required_review:\s*$/.test(line);
      blockIndent = null;
      continue;
    }
    if (!inRequiredReview || !line.trim()) {
      continue;
    }
    const indent = line.length - line.trimStart().length;
    if (blockIndent !== null) {
      const item = line.match(/^\s+-\s*(.+?)\s*$/);
      if (indent > blockIndent && item) {
        ids.push(unquoteYamlScalar(item[1]));
        continue;
      }
      blockIndent = null;
    }
    const flow = line.match(/^\s+-?\s*profiles:\s*\[([^\]]*)\]\s*$/);
    if (flow) {
      ids.push(...flow[1].split(",").map(unquoteYamlScalar));
      continue;
    }
    if (/^\s+-?\s*profiles:\s*$/.test(line)) {
      blockIndent = indent;
    }
  }
  return ids.filter(Boolean);
}

function unquoteYamlScalar(value) {
  return String(value).trim().replace(/^["']|["']$/g, "");
}

// 감지된 profile은 선택과 무관하게 항상 깐다. kit.json의 `profile` 필드가 그 값을
// 기록하고, worktree에는 kit.json이 없어서 `active_profile_ids`가 `detect_profile`로
// 떨어진다(`cli.py`의 `gates`/`architecture-lint --worktree`). 그때 감지 id의 YAML이
// 없으면 `load_profile`이 `unknown profile`로 던져 gate phase가 통째로 실패한다.
export function activeInstallProfileIds(detectedProfile, installSelection) {
  const selected = Array.isArray(installSelection?.profiles) ? installSelection.profiles : [];
  return [detectedProfile, ...selected].filter((value) => typeof value === "string" && value);
}

// 이전 설치본이 받아 둔 남의 stack profile을 걷어낸다. kit이 배포하는 이름만
// 지운다 — 사용자가 만든 custom profile은 `src`에 없으므로 살아남는다.
//
// 내용이 지금 배포본과 같으면 잃는 것이 없으니 조용히 지운다. runtime 사본은
// 매 install이 전부 다시 깔고 여기서 다시 걷어내므로, 이 침묵이 없으면 재설치마다
// 같은 알림이 stack 수만큼 되풀이된다. 다르면 사용자가 손댔을 수 있으므로
// 사본을 남기고 알린다.
export function pruneUninstalledProfiles(root, src, dest, keepNames) {
  if (!fs.existsSync(dest)) {
    return 0;
  }
  let pruned = 0;
  for (const entry of fs.readdirSync(dest, { withFileTypes: true })) {
    if (!entry.isFile() || keepNames.has(entry.name)) {
      continue;
    }
    const bundledPath = path.join(src, entry.name);
    if (!fs.existsSync(bundledPath)) {
      continue;
    }
    const target = path.join(dest, entry.name);
    if (fs.readFileSync(target, "utf8") !== fs.readFileSync(bundledPath, "utf8")) {
      const backup = writePruneBackup(target);
      console.log(
        `${PRUNE_NOTICE_PREFIX}${path.relative(root, target)}` +
          ` (backup: ${path.relative(root, backup)})`,
      );
    }
    fs.rmSync(target);
    pruned += 1;
  }
  return pruned;
}

export function arrayValue(value) {
  return Array.isArray(value) ? value.map(String) : [];
}

export function backupIfDifferent(root, target, content) {
  if (!fs.existsSync(target)) {
    return;
  }
  if (fs.readFileSync(target, "utf8") === content) {
    return;
  }
  // 덮어쓰는 내용을 잃지 않는다. 고정 이름 하나만 쓰면 둘 중 하나를 반드시
  // 버리게 된다 — 매번 덮으면 사용자 원본이, 안 덮으면 이번 편집이 사라진다.
  const backup = nextFreeBackupPath(`${target}.bak`, fs.readFileSync(target, "utf8"));
  if (backup === null) {
    return;  // 같은 내용이 이미 백업돼 있다.
  }
  fs.copyFileSync(target, backup);
  console.log(`  ~ replaced ${path.relative(root, target)} (backup: ${path.relative(root, backup)})`);
}

export function ensureChildPath(parent, child) {
  const parentResolved = path.resolve(parent);
  const childResolved = path.resolve(child);
  const relative = path.relative(parentResolved, childResolved);
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(`path escapes parent: ${child}`);
  }
}

export function escapeRegex(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function hasChildWithSuffix(rootDir, suffix) {
  if (!fs.existsSync(rootDir)) {
    return false;
  }
  return fs.readdirSync(rootDir).some((name) => name.endsWith(suffix));
}

export function isGitignoreEntryCovered(entry, existing) {
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

export function makeHooksExecutable(root) {
  const hooksDir = path.join(root, ".agent-flow", "scripts", "hooks");
  if (!fs.existsSync(hooksDir)) {
    return;
  }
  for (const entry of fs.readdirSync(hooksDir)) {
    if (entry.endsWith(".sh") || entry.endsWith(".py")) {
      fs.chmodSync(path.join(hooksDir, entry), 0o755);
    }
  }
}

export function nextFreeBackupPath(base, content) {
  // 이미 같은 내용이 백업돼 있으면 사본을 늘리지 않는다. 다르면 비어 있는
  // 다음 이름을 찾는다. 무엇도 덮지 않으므로 어떤 버전도 잃지 않는다.
  for (let i = 0; i < 100; i += 1) {
    const candidate = i === 0 ? base : `${base}.${i}`;
    if (!fs.existsSync(candidate)) {
      return candidate;
    }
    if (fs.readFileSync(candidate, "utf8") === content) {
      return null;
    }
  }
  return null;
}

export function planReviewerSkillMarkdown() {
  return `---\nname: plan-reviewer\ndescription: Use during the full-feature plan-review phase.\n---\n\n# Plan Reviewer\n\nUse during the full-feature plan-review phase.\n\nReview only. Do not rewrite the plan.\n\nCheck:\n\n- Missing data collection steps.\n- Missing validation steps.\n- Wrong implementation order.\n- Oversized slices that should be split.\n- Missing state/storage steps.\n- Test coverage gaps.\n- Architecture risks before coding.\n\nArtifact template:\n\n# Plan Review\n\nverdict: approve | request-changes\n\n## Scope Checked\n\n## Missing Steps\n\n## Wrong Order\n\n## Oversized Slices\n\n## Validation Gaps\n\n## Data/State Gaps\n\n## Architecture Risks\n\n## Required Changes\n\n## Approval Notes\n`;
}

export function pruneRetiredManagedScripts(root) {
  const scriptsDir = path.join(root, ".agent-flow", "scripts");
  for (const scriptName of ["check-context-docs.mjs", "check-context-docs.ts"]) {
    const target = path.join(scriptsDir, scriptName);
    if (!fs.existsSync(target) || !fs.statSync(target).isFile()) {
      continue;
    }
    const kept = nextFreeBackupPath(`${target}.removed`, fs.readFileSync(target, "utf8"));
    if (kept !== null) {
      fs.copyFileSync(target, kept);
      fs.chmodSync(kept, 0o644);
    }
    fs.rmSync(target, { force: true });
    console.log(`  - removed retired script: ${path.relative(root, target)}`);
  }
}

export function readHookSettings(settingsPath) {
  if (!fs.existsSync(settingsPath)) {
    return {};
  }
  try {
    return JSON.parse(fs.readFileSync(settingsPath, "utf8"));
  } catch {
    const backupPath = `${settingsPath}.bak`;
    fs.copyFileSync(settingsPath, backupPath);
    console.error(`warning: could not parse ${settingsPath}; backed up to ${backupPath} before overwriting`);
    return {};
  }
}

export function removeGitignoreEntries(pathName, entries) {
  if (!fs.existsSync(pathName)) return;
  const removals = new Set(entries);
  const current = fs.readFileSync(pathName, "utf8");
  const lines = current.split(/\r?\n/);
  const filtered = lines.filter((line) => !removals.has(line.trim()));
  if (filtered.length === lines.length) return;
  const next = `${filtered.join("\n").replace(/\n*$/, "")}\n`;
  fs.writeFileSync(pathName, next, "utf8");
}

export function removeLegacyProjectSkillCopies(projectRoot, skillName) {
  for (const parent of [
    path.join(projectRoot, ".agent-flow", "skills"),
    path.join(projectRoot, ".claude", "skills"),
    path.join(projectRoot, ".codex", "skills"),
    path.join(projectRoot, ".Codex", "skills"),
    path.join(projectRoot, ".omp", "skills"),
    path.join(projectRoot, ".gemini", "skills"),
    path.join(projectRoot, ".gemini", "antigravity", "skills"),
  ]) {
    fs.rmSync(path.join(parent, skillName), { recursive: true, force: true });
  }
}

export function shellQuote(value) {
  return `'${String(value).replaceAll("'", "'\\''")}'`;
}

export function tomlBasicString(value) {
  return String(value).replaceAll("\\", "\\\\").replaceAll("\"", "\\\"");
}

export function uniqueStrings(values) {
  return [...new Set(values.map(String).filter(Boolean))];
}

export function unquoteShellWord(value) {
  if (typeof value !== "string") {
    return "";
  }
  if (value.startsWith("'") && value.endsWith("'")) {
    return value.slice(1, -1).replaceAll("'\\''", "'");
  }
  return value;
}

export function upsertGitignore(pathName, entries) {
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

export function validateSkillDependencies(skills) {
  const names = new Set(skills.map((skill) => skill.name));
  const warnings = [];
  for (const skill of skills) {
    for (const required of skill.requires || []) {
      if (!names.has(required)) {
        warnings.push(`${skill.name}: missing required skill ${required}`);
      }
    }
  }
  return warnings;
}

export const AGENT_FLOW_COMMAND = "agent-flow";

export const HOME = process.env.HOME || process.env.USERPROFILE || "";

export const KIT_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

export const PRUNE_BACKUP_SUFFIX = ".removed";

export const PRUNE_BACKUP_VERSIONED = /\.removed\.[0-9a-f]{8}$/;

export const SKILL_INDEX_START = "<!-- agent-flow:skills:start -->";

export const SKILL_INDEX_END = "<!-- agent-flow:skills:end -->";

// Read 계열 tool 이름은 host마다 다르다. comment-checker의 write matcher와 같은 방식으로 합집합을 쓴다.
export const READ_TOOL_MATCHER = "^(Read|read|read_file|view|cat)$";

// 셸 실행 tool도 host마다 이름이 다르다. 관측 전용이라 PostToolUse에만 붙는다.
export const COMMAND_TOOL_MATCHER = "^(Bash|bash|shell|run_terminal_cmd|execute_command|local_shell|terminal)$";

export const SPEC_PREPARE_TOOL_MATCHER = "^(apply_patch|Write|Edit|MultiEdit|write|edit|multi_edit|multiedit|Bash|bash|shell|run_terminal_cmd|execute_command|local_shell|terminal)$";

export function architectureReviewerSkillMarkdown() {
  return `---\nname: architecture-reviewer\ndescription: Use during the full-feature architecture-review phase.\n---\n\n# Architecture Reviewer\n\nUse during the full-feature architecture-review phase.\n\nReview implemented code against domain decisions and DDD/Clean Architecture. Run two independent active-host reviewer sub-agents before approve. Each reviewer section must include \`reviewer-source: sub-agent\`; optional cross-host reviewers are extra evidence and do not replace active-host reviewers.\n\nArtifact template:\n\n# Architecture Review\n\n## Reviewer 1\nreviewer-source: sub-agent\nverdict: approve | request-changes\n\n## Findings\n\n## Domain Alignment\n\n## Layer Violations\n\n## Repository Boundary Issues\n\n## Dependency Direction Issues\n\n## Required Refactors\n\n## Approved Exceptions\n\n## Reviewer 2\nreviewer-source: sub-agent\nverdict: approve | request-changes\n\n## Findings\n\n## Overall\nverdict: approve | request-changes\n\n## Completion Gate\nskills_checked: true\nprofile-skill-selection: applied\nactive-profiles: <profile list>\nchanged-file-skill-resolution: applied\nrequired-profile-skills: checked\nmissing-required-profile-skills: none|<list>\narchitecture-contract-check: pass|fail|n/a\ncodex-claude-parity-check: pass|fail\nhook-parity-check: pass|fail\nclean-architecture: applied\nproject-local-skills: checked|n/a\nproject-local-skills-used: <skill list or n/a>\ndependency-rule: pass|fail\nusecase-boundary: pass|fail|n/a\nusecase-calls-usecase: pass|fail\nrepository-boundary: pass|fail\ncache-boundary: pass|fail|n/a\nmemory-disk-cache-separated: pass|fail|n/a\nmapping-boundary: pass|fail|n/a\ndto-entity-domain-ui-separated: pass|fail\nsolid-boundary-check: pass|fail\npresentation-skill: android|react|react-native|ios|n/a\npresentation-state-review: pass|fail|n/a\nui-state-modeling: explicit|n/a\npresentation-mapping-boundary: domain-to-uimodel|n/a\ndi-boundary: hilt|context-provider|tsyringe|swift-environment|factory|swift-dependencies|swinject|needle|direct|existing|n/a\n`;
}

export function fullFeatureSkillMarkdown() {
  return `---\nname: full-feature-workflow\ndescription: Use this skill for feature work in this project.\n---\n\n# Full Feature Workflow\n\nUse this skill for feature work in this project.\n\nAlways drive progress through the runner output. Run \`${AGENT_FLOW_COMMAND} status\`, then execute the printed \`next_command\` exactly.\n\nDo not skip phases. If existing docs satisfy a phase, write the required artifact and reference those docs. If a gate, review, PR comment, or PR check fails, complete the matching fix phase and push again before merge/handoff.\n\nApply \`code-generation-discipline\` during code and review phases. Resolve required skills from active profile metadata, installed skill index, changed files, and task scope before writing or judging code.\n`;
}

export function productBriefSkillMarkdown() {
  return `---\nname: product-brief\ndescription: Use during the full-feature product-brief phase.\n---\n\n# Product Brief\n\nUse during the full-feature product-brief phase.\n\nAsk YC-style forcing questions before implementation:\n\n1. Demand Reality: what behavior proves people want this?\n2. Status Quo: how do they solve it today?\n3. Desperate Specificity: who is the most painful target user?\n4. Narrowest Wedge: what is the smallest version worth using now?\n5. Observation: what concrete user behavior was observed?\n6. Future Fit: why is now the right time?\n\nArtifact template:\n\n# Product Brief\n\n## Mode\nstartup | builder | internal\n\n## Demand Evidence\n\n## Status Quo\n\n## Target User\n\n## Narrowest Wedge\n\n## Observed Behavior\n\n## Why Now\n\n## Cut List\n\n## Assignment\n\n## Decision\nbuild | defer | cut\n`;
}

export function pushWatchSkillMarkdown() {
  return `---\nname: push-watch\ndescription: Use this skill after local verification is complete and the branch is ready to publish.\n---\n\n# Push Watch\n\nUse this skill after local verification is complete and the branch is ready to publish.\n\nRun:\n\n\`\`\`bash\n${AGENT_FLOW_COMMAND} run push-watch\n\`\`\`\n\nFlow:\n\n1. Sanity check the branch and working tree.\n2. Commit and push the current branch.\n3. Open or record the pull request.\n4. Watch PR checks and review threads.\n5. Route failures through \`pr-comment-fix\` or \`pr-ci-fix\`; comment fixes must also resolve the corresponding GitHub review threads.\n6. Push again and return to \`pr-watch\`.\n7. When checks and comments are green, route to \`merge\`.\n\nRules:\n\n- Protected branches are blocked: main, master, develop.\n- Record PR watch state with \`status: green\`, \`status: comments\`, \`status: ci-failed\`, or \`status: pending\`.\n- merge requires explicit approval. Do not merge unattended.\n`;
}

export function hookScriptCommand(root, scriptName) {
  const scriptPath = shellQuote(path.join(root, ".agent-flow", "scripts", "hooks", scriptName));
  if (scriptName.endsWith(".py")) {
    return `/usr/bin/python3 -I ${scriptPath}`;
  }
  return `/bin/bash ${scriptPath}`;
}

export function isPruneBackupName(name) {
  return name.endsWith(PRUNE_BACKUP_SUFFIX) || PRUNE_BACKUP_VERSIONED.test(name);
}

export function writePruneBackup(target) {
  const content = fs.readFileSync(target);
  const primary = `${target}${PRUNE_BACKUP_SUFFIX}`;
  if (!fs.existsSync(primary)) {
    fs.writeFileSync(primary, content);
    return primary;
  }
  if (fs.readFileSync(primary).equals(content)) {
    return primary;
  }
  const digest = crypto.createHash("sha256").update(content).digest("hex").slice(0, 8);
  const versioned = `${primary}.${digest}`;
  if (!fs.existsSync(versioned) || !fs.readFileSync(versioned).equals(content)) {
    fs.writeFileSync(versioned, content);
  }
  return versioned;
}

export function managedHookScriptName(command) {
  const normalized = unquoteShellWord(command).replaceAll("\\", "/").replaceAll("'", "").replaceAll('"', "");
  for (const scriptName of MANAGED_HOOK_SCRIPTS) {
    if (
      normalized === `.agent-flow/scripts/hooks/${scriptName}` ||
      normalized === `scripts/hooks/${scriptName}` ||
      normalized.endsWith(`/.agent-flow/scripts/hooks/${scriptName}`) ||
      normalized.endsWith(`/scripts/hooks/${scriptName}`) ||
      normalized.includes(`/.agent-flow/scripts/hooks/${scriptName}`) ||
      normalized.includes(`/scripts/hooks/${scriptName}`)
    ) {
      return scriptName;
    }
  }
  return null;
}

export function managedHookDigests() {
  return Object.fromEntries(
    MANAGED_HOOK_SCRIPTS.map((name) => [
      name,
      crypto
        .createHash("sha256")
        .update(fs.readFileSync(path.join(KIT_ROOT, "scripts", "hooks", name)))
        .digest("hex"),
    ]),
  );
}

export function codexConfigPath() {
  if (!HOME) {
    return null;
  }
  return path.join(HOME, ".codex", "config.toml");
}

export function ompExtensionIsKitOwned(target) {
  if (!fs.existsSync(target)) {
    return true;
  }
  const current = fs.readFileSync(target, "utf8");
  // 표식은 이번 버전부터 붙는다. 그 이전 설치본에는 없으므로 생성 서명으로도
  // 인정한다. 이게 없으면 기존 사용자는 첫 업그레이드에서 영영 막힌다.
  return (
    current.includes(OMP_EXTENSION_MARKER) ||
    current.includes("export default function agentFlowHooks(")
  );
}

export function removeOmpHooksExtension(root) {
  const target = path.join(root, ".omp", "extensions", "agent-flow-hooks.ts");
  if (!fs.existsSync(target)) {
    return;
  }
  if (!ompExtensionIsKitOwned(target)) {
    console.warn(`agent-flow: ${path.relative(root, target)} is not kit-managed; leaving it alone.`);
    return;
  }
  const kept = nextFreeBackupPath(`${target}.removed`, fs.readFileSync(target, "utf8"));
  if (kept !== null) {
    fs.copyFileSync(target, kept);
  }
  fs.rmSync(target, { force: true });
  console.log(`  - hooks disabled: removed ${path.relative(root, target)}`);
}

export function removeCodexBroadTrustState(root) {
  const configPath = codexConfigPath();
  if (!configPath || !fs.existsSync(configPath)) {
    return;
  }
  const tableHeader = `[projects."${tomlBasicString(root)}"]`;
  const tableName = tableHeader.slice(1, -1);
  const tablePattern = new RegExp(
    `(^|\\n)\\s*\\[\\s*${escapeRegex(tableName)}\\s*\\]\\s*(?:#.*)?\\n`
      + "([\\s\\S]*?)(?=\\n\\s*\\[[^\\n]+\\]|$)",
  );
  const trustPattern = /(^|\n)\s*trust_level\s*=\s*"trusted"\s*(?:#.*)?(?=\n|$)/;
  const current = fs.readFileSync(configPath, "utf8");
  const next = current.replace(tablePattern, (full, leading, body) => {
    if (!trustPattern.test(body)) {
      return full;
    }
    const kept = body.replace(trustPattern, "$1");
    if (!kept.trim()) {
      return leading;
    }
    return `${leading}${tableHeader}\n${kept.replace(/^\n/, "")}`;
  });
  if (next !== current) {
    fs.writeFileSync(configPath, next.endsWith("\n") ? next : `${next}\n`, "utf8");
  }
}

export function safeSkillName(value) {
  const candidate = String(value).trim();
  return /^[A-Za-z0-9._-]+$/.test(candidate) && !candidate.startsWith(".") && !candidate.includes("..") && candidate !== "."
    ? candidate
    : String(candidate || "skill")
        .toLowerCase()
        .replace(/[^a-z0-9_-]+/g, "-")
        .replace(/^-+|-+$/g, "") || "skill";
}

export function skillRequires(name) {
  return SKILL_DEPENDENCIES.get(name) || [];
}

export function readJsonIfExists(pathName) {
  if (!fs.existsSync(pathName)) {
    return null;
  }
  try {
    return JSON.parse(fs.readFileSync(pathName, "utf8"));
  } catch {
    return null;
  }
}

export function retiredHookScripts(hooksDisabled) {
  return hooksDisabled
    ? [...RETIRED_MANAGED_HOOK_SCRIPTS, ...MANAGED_HOOK_SCRIPTS]
    : RETIRED_MANAGED_HOOK_SCRIPTS;
}

export function isRetiredHookCommand(command, hooksDisabled) {
  if (typeof command !== "string" || !command) {
    return false;
  }
  const normalized = unquoteShellWord(command).replaceAll("\\", "/").replaceAll("'", "").replaceAll('"', "");
  return retiredHookScripts(hooksDisabled).some(
    (name) => normalized.endsWith(`/scripts/hooks/${name}`) || normalized === `scripts/hooks/${name}`,
  );
}

export function pruneRetiredHooks(settings, replaceManaged, hooksDisabled) {
  if (!settings || typeof settings !== "object" || !settings.hooks) {
    return false;
  }
  let changed = false;
  for (const [event, entries] of Object.entries(settings.hooks)) {
    if (!Array.isArray(entries)) {
      continue;
    }
    for (const entry of entries) {
      if (!Array.isArray(entry?.hooks)) {
        continue;
      }
      const kept = entry.hooks.filter(
        (hook) => !isRetiredHookCommand(hook?.command, hooksDisabled)
          && !(replaceManaged && managedHookScriptName(hook?.command)),
      );
      if (kept.length !== entry.hooks.length) {
        entry.hooks = kept;
        changed = true;
      }
    }
    const nonEmpty = entries.filter((entry) => !Array.isArray(entry?.hooks) || entry.hooks.length > 0);
    if (nonEmpty.length !== entries.length) {
      settings.hooks[event] = nonEmpty;
      changed = true;
    }
  }
  return changed;
}

export function pruneRetiredHookScripts(root, hooksDisabled) {
  // 설정에서만 빼면 실행 파일이 디스크에 남는다. 남은 파일을 다른 경로가
  // 다시 집어 실행하면 은퇴시킨 guard가 되살아난다.
  const hooksDir = path.join(root, ".agent-flow", "scripts", "hooks");
  for (const scriptName of retiredHookScripts(hooksDisabled)) {
    const target = path.join(hooksDir, scriptName);
    if (fs.existsSync(target)) {
      // 사용자가 같은 이름으로 자기 스크립트를 뒀을 수 있다. 되돌릴 수 있게
      // 사본을 남기고 지운다. 설치본이 관리하지 않는 host 설정이 이 경로를
      // 여전히 가리킬 수 있으므로 경로를 함께 알린다.
      const kept = nextFreeBackupPath(`${target}.removed`, fs.readFileSync(target, "utf8"));
      if (kept !== null) {
        fs.copyFileSync(target, kept);
        // 실행 권한은 떼어 둔다. 되살릴 수 있게 남기는 사본이지 실행 대상이 아니다.
        fs.chmodSync(kept, 0o644);
      }
      fs.rmSync(target, { force: true });
      console.log(
        `  - removed retired hook: ${path.relative(root, target)} ` +
          `(backup: ${path.relative(root, target)}.removed)`,
      );
    }
  }
}

export function mergeHookSettings(settings, desired, hooksDisabled) {
  if (!settings.hooks) {
    settings.hooks = {};
  }
  pruneRetiredHooks(settings, true, hooksDisabled);
  for (const [event, entries] of Object.entries(desired)) {
    if (!settings.hooks[event]) {
      settings.hooks[event] = [];
    }
    for (const entry of entries) {
      const existing = settings.hooks[event].find((e) => (e.matcher ?? "") === (entry.matcher ?? ""));
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
}

export function mergeHookConfig(settings, source, hooksDisabled) {
  if (!source || typeof source !== "object") {
    return;
  }
  for (const [key, value] of Object.entries(source)) {
    if (key !== "hooks" && settings[key] === undefined) {
      settings[key] = value;
    }
  }
  if (source.hooks) {
    mergeHookSettings(settings, source.hooks, hooksDisabled);
  }
}

export function claudeHooksSettings(root) {
  return {
    hooks: {
      UserPromptSubmit: [
        {
          hooks: [
            { type: "command", command: hookScriptCommand(root, "prepare-spec-user-prompt.py") },
            { type: "command", command: hookScriptCommand(root, "confirm-spec-user-prompt.py") },
          ],
        },
      ],
      PreToolUse: [
        {
          matcher: "Bash",
          hooks: [
            { type: "command", command: hookScriptCommand(root, "guard-protected-branch.sh") },
          ],
        },
        {
          matcher: SPEC_PREPARE_TOOL_MATCHER,
          hooks: [
            { type: "command", command: hookScriptCommand(root, "guard-host-worktree.sh") },
            { type: "command", command: hookScriptCommand(root, "guard-spec-approval.sh") },
          ],
        },
      ],
      PostToolUse: [
        {
          matcher: "^(apply_patch|Write|Edit|MultiEdit|write|edit|multi_edit|multiedit)$",
          hooks: [
            { type: "command", command: hookScriptCommand(root, "comment-checker.py") },
            { type: "command", command: hookScriptCommand(root, "guard-host-worktree.sh") },
          ],
        },
        {
          matcher: SPEC_PREPARE_TOOL_MATCHER,
          hooks: [
            { type: "command", command: hookScriptCommand(root, "prepare-spec-user-prompt.py") },
          ],
        },
        {
          matcher: READ_TOOL_MATCHER,
          hooks: [{ type: "command", command: hookScriptCommand(root, "record-skill-read.py") }],
        },
        {
          matcher: COMMAND_TOOL_MATCHER,
          hooks: [
            { type: "command", command: hookScriptCommand(root, "record-command-run.py") },
            { type: "command", command: hookScriptCommand(root, "bind-host-worktree.py") },
            { type: "command", command: hookScriptCommand(root, "guard-host-worktree.sh") },
            { type: "command", command: hookScriptCommand(root, "worktree-tripwire.py") },
          ],
        },
      ],
      Stop: [
        {
          hooks: [{ type: "command", command: hookScriptCommand(root, "show-phase-status.sh") }],
        },
      ],
    },
  };
}

export function codexHooksSettings(root) {
  return {
    hooks: {
      UserPromptSubmit: [
        {
          hooks: [
            { type: "command", command: hookScriptCommand(root, "prepare-spec-user-prompt.py") },
            { type: "command", command: hookScriptCommand(root, "confirm-spec-user-prompt.py") },
          ],
        },
      ],
      PreToolUse: [
        {
          matcher: "Bash",
          hooks: [
            { type: "command", command: hookScriptCommand(root, "guard-protected-branch.sh") },
          ],
        },
        {
          matcher: SPEC_PREPARE_TOOL_MATCHER,
          hooks: [
            { type: "command", command: hookScriptCommand(root, "guard-host-worktree.sh") },
            { type: "command", command: hookScriptCommand(root, "guard-spec-approval.sh") },
          ],
        },
      ],
      PostToolUse: [
        {
          matcher: "^(apply_patch|Write|Edit|MultiEdit|write|edit|multi_edit|multiedit)$",
          hooks: [
            { type: "command", command: hookScriptCommand(root, "comment-checker.py") },
            { type: "command", command: hookScriptCommand(root, "guard-host-worktree.sh") },
          ],
        },
        {
          matcher: SPEC_PREPARE_TOOL_MATCHER,
          hooks: [
            { type: "command", command: hookScriptCommand(root, "prepare-spec-user-prompt.py") },
          ],
        },
        {
          matcher: READ_TOOL_MATCHER,
          hooks: [{ type: "command", command: hookScriptCommand(root, "record-skill-read.py") }],
        },
        {
          matcher: COMMAND_TOOL_MATCHER,
          hooks: [
            { type: "command", command: hookScriptCommand(root, "record-command-run.py") },
            { type: "command", command: hookScriptCommand(root, "bind-host-worktree.py") },
            { type: "command", command: hookScriptCommand(root, "guard-host-worktree.sh") },
            { type: "command", command: hookScriptCommand(root, "worktree-tripwire.py") },
          ],
        },
      ],
      Stop: [
        {
          hooks: [{ type: "command", command: hookScriptCommand(root, "show-phase-status.sh") }],
        },
      ],
    },
  };
}

export function skillIndexBlock(root) {
  const index = readJsonIfExists(path.join(root, ".agent-flow", "skills", "index.json"));
  const skills = Array.isArray(index?.skills) ? index.skills : [];
  if (skills.length === 0) {
    // 인덱스가 없는 설치본에서 거짓 목록을 쓰지 않는다. 빈 인덱스는 "아직
    // 모른다"이지 "skill이 없다"가 아니다.
    return [
      SKILL_INDEX_START,
      `- 설치된 skill 인덱스가 아직 없다. \`${AGENT_FLOW_COMMAND} skills sync\` 후 다시 생성된다.`,
      SKILL_INDEX_END,
    ].join("\n");
  }
  const names = (delivery) =>
    skills
      .filter((skill) => (skill.delivery === "passive") === (delivery === "passive"))
      .map((skill) => String(skill.name))
      .sort((a, b) => a.localeCompare(b));
  const lines = [
    SKILL_INDEX_START,
    "```text",
    "[agent-flow skill index]|root: .agent-flow/skills",
    "|IMPORTANT: 아래 파일이 기억보다 우선한다. 변경 대상을 먼저 훑고, scope가 걸리는 것만 읽는다.",
  ];
  const passive = names("passive");
  if (passive.length > 0) {
    lines.push(`|always:{${passive.join(",")}}`);
  }
  const onDemand = names("on-demand");
  if (onDemand.length > 0) {
    lines.push(`|on-demand:{${onDemand.join(",")}}`);
  }
  lines.push("```", SKILL_INDEX_END);
  return lines.join("\n");
}

export function upsertSkillIndexBlock(root) {
  const block = skillIndexBlock(root);
  for (const fileName of ["AGENTS.md", "CLAUDE.md"]) {
    const target = path.join(root, fileName);
    if (!fs.existsSync(target)) continue;
    const current = fs.readFileSync(target, "utf8");
    const start = current.indexOf(SKILL_INDEX_START);
    const end = current.indexOf(SKILL_INDEX_END);
    if (start === -1 || end === -1 || end < start) continue;
    const next = current.slice(0, start) + block + current.slice(end + SKILL_INDEX_END.length);
    if (next !== current) {
      fs.writeFileSync(target, next, "utf8");
    }
  }
}
