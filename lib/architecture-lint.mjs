import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

const SOURCE_SUFFIXES = new Set([
  ".gradle", ".kt", ".kts", ".java", ".swift", ".py", ".ts", ".tsx", ".js", ".jsx",
]);
const IGNORED_PARTS = new Set([
  ".agent-flow", ".git", ".gradle", ".idea", "build", "node_modules", "__pycache__",
  "__tests__", "tests", "test", "androidTest", "commonTest", "iosTest",
]);
const TEST_NAME_RE = /(^test_|[_-]test$|[_-]test\.|testcase|tests?$)/i;
const CORE_FAMILY_SEGMENTS = new Set([
  "data", "design-system", "designsystem", "domain", "navigation", "network", "permission",
  "platform", "resources", "ui",
]);
const PLACEHOLDER_RESERVED_SEGMENTS = new Set(["src", "build", "gradle", ".gradle"]);

export function lintProject(root, profileId, { files = null, loadProfile } = {}) {
  if (typeof loadProfile !== "function") throw new Error("architecture lint requires a profile loader");
  const profile = loadProfile(profileId);
  const architecture = isMapping(profile?.architecture) ? profile.architecture : null;
  if (!architecture || !Array.isArray(architecture.roles)) return [];
  const candidates = files === null ? changedFiles(root) : files;
  const normalizedCandidates = normalizedCandidateFiles(candidates);
  if (!architectureLintIsActive(root, architecture, normalizedCandidates)) return [];
  const findings = [];
  const managedRoots = architectureManagedRoots(architecture.roles);
  for (const relPath of normalizedCandidates) {
    const match = matchRole(relPath, architecture.roles);
    if (!match) {
      if (managedRoots.length > 0 && !isRootGradleConfig(relPath)) {
        findings.push(finding(relPath, "path is outside profile architecture role mapping"));
      }
      continue;
    }
    const candidate = path.join(root, ...relPath.split("/"));
    const text = isRegularFile(candidate) ? fs.readFileSync(candidate, "utf8") : "";
    findings.push(...validateForbiddenTokens(relPath, text, match.role));
    findings.push(...validatePackageSuffix(relPath, text, match.role, match.captures));
    findings.push(...validateGradleNamespace(root, relPath, match));
    findings.push(...validateDeclaredModules(root, relPath, match.role, match.captures));
    findings.push(...validateGradleDependencies(root, relPath, match));
    findings.push(...validatePair(root, relPath, architecture.roles, match));
  }
  return findings;
}

export function lintProfiles(root, profileIds, { files = null, loadProfile } = {}) {
  if (typeof loadProfile !== "function") throw new Error("architecture lint requires a profile loader");
  const candidates = normalizedCandidateFiles(files === null ? changedFiles(root) : files);
  const expanded = expandedLintProfileIds(profileIds, candidates);
  if (expanded.length <= 1) {
    return Object.fromEntries(expanded.map((profileId) => [
      profileId,
      lintProject(root, profileId, { files, loadProfile }),
    ]));
  }
  const contexts = new Map(expanded.map((profileId) => [
    profileId,
    profileLintContext(profileId, loadProfile),
  ]));
  const selected = new Map(expanded.map((profileId) => [profileId, []]));
  for (const relPath of candidates) {
    let relevant = expanded.filter((profileId) => contextPathIsRelevant(relPath, contexts.get(profileId)));
    if (relevant.length === 0) {
      const fallback = firstProfileWithRoles(expanded, contexts);
      relevant = fallback ? [fallback] : [];
    }
    for (const profileId of relevant) selected.get(profileId).push(relPath);
  }
  return Object.fromEntries(expanded.map((profileId) => [
    profileId,
    lintProject(root, profileId, { files: selected.get(profileId), loadProfile }),
  ]));
}

export function changedFiles(root) {
  if (!fs.existsSync(path.join(root, ".git"))) return [];
  const tracked = spawnSync("git", ["diff", "--name-only", "--diff-filter=ACMRTUXB", "HEAD", "--"], {
    cwd: root,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
    timeout: 30_000,
  });
  const untracked = spawnSync("git", ["ls-files", "--others", "--exclude-standard"], {
    cwd: root,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
    timeout: 30_000,
  });
  const result = [];
  if (!tracked.error && tracked.status === 0) result.push(...nonemptyLines(tracked.stdout));
  if (!untracked.error && untracked.status === 0) result.push(...nonemptyLines(untracked.stdout));
  return [...new Set(result)];
}

export function normalizedCandidateFiles(files) {
  const normalized = [];
  for (const fileName of files ?? []) {
    const relPath = String(fileName).replaceAll("\\", "/").replace(/^\/+|\/+$/g, "");
    if (!relPath || relPath.split("/").some((part) => IGNORED_PARTS.has(part))) continue;
    if (isTestFile(relPath) || !SOURCE_SUFFIXES.has(path.posix.extname(relPath))) continue;
    normalized.push(relPath);
  }
  return normalized;
}

export function matchRole(relPath, roles) {
  let best = null;
  for (const role of roles ?? []) {
    if (!isMapping(role) || !Array.isArray(role.paths)) continue;
    for (const pattern of role.paths) {
      if (typeof pattern !== "string") continue;
      const captures = matchPattern(relPath, pattern);
      if (captures === null) continue;
      const candidate = { role, captures, pattern };
      if (!best || compareSpecificity(patternSpecificity(pattern), patternSpecificity(best.pattern)) > 0) {
        best = candidate;
      }
    }
  }
  return best;
}

export function matchPattern(relPath, pattern) {
  const pathParts = relPath.split("/");
  const patternParts = pattern.replace(/^\/+|\/+$/g, "").split("/");
  if (pathParts.length < patternParts.length) return null;
  const captures = {};
  for (let index = 0; index < patternParts.length; index += 1) {
    const expected = patternParts[index];
    const actual = pathParts[index];
    const token = expected.match(/^<([A-Za-z][A-Za-z0-9_-]*)>$/);
    if (token) {
      if (PLACEHOLDER_RESERVED_SEGMENTS.has(actual)) return null;
      captures[token[1]] = actual;
    } else if (expected !== actual) {
      return null;
    }
  }
  return captures;
}

export function architectureManagedRoots(roles) {
  const roots = new Set();
  for (const role of roles ?? []) {
    if (!isMapping(role) || !Array.isArray(role.paths)) continue;
    for (const pattern of role.paths) {
      if (typeof pattern !== "string") continue;
      const root = staticPrefixBeforePlaceholder(pattern);
      if (!root) continue;
      roots.add(root);
      const parent = architectureFamilyParent(root);
      if (parent) roots.add(parent);
    }
  }
  return [...roots].sort((left, right) => right.length - left.length || compareCodePoints(left, right));
}

export function validateForbiddenTokens(relPath, text, role) {
  if (!Array.isArray(role?.forbidden)) return [];
  const haystacks = [path.posix.basename(relPath), text].map((value) => value.toLowerCase());
  return role.forbidden.flatMap((token) => {
    if (typeof token !== "string" || !token) return [];
    return haystacks.some((value) => value.includes(token.toLowerCase()))
      ? [finding(relPath, `${role.id ?? "role"} contains forbidden token ${token}`)]
      : [];
  });
}

export function validatePackageSuffix(relPath, text, role, captures) {
  const suffix = role?.package_suffix;
  if (typeof suffix !== "string" || !suffix || isGradleBuildFile(relPath)) return [];
  const packageName = packageFromSource(text);
  if (!packageName) return [finding(relPath, `${role.id ?? "role"} requires package declaration`)];
  let expected = suffix;
  for (const [key, value] of Object.entries(captures)) {
    expected = expected.replaceAll(`<${key}>`, packageSegment(value));
  }
  return `.${packageName}.`.includes(`.${expected}.`)
    ? []
    : [finding(relPath, `package ${packageName} does not match role suffix ${expected}`)];
}

export function typeSafeProjectDependencies(text) {
  const modules = new Set();
  for (const match of text.matchAll(/\bprojects((?:\.[A-Za-z_][A-Za-z0-9_]*)+)/g)) {
    const parts = match[1].split(".").filter(Boolean);
    if (parts.length === 0) continue;
    modules.add(`:${parts.join(":")}`);
    modules.add(`:${parts.map(gradleAccessorSegmentToModule).join(":")}`);
  }
  return modules;
}

export function gradleAccessorSegmentToModule(segment) {
  return segment.replace(/([a-z0-9])([A-Z])/g, "$1-$2").replaceAll("_", "-").toLowerCase();
}

export function packageFromSource(text) {
  for (const line of String(text).split(/\r?\n/)) {
    const match = line.match(/^\s*package\s+([A-Za-z_][A-Za-z0-9_.]*)\s*;?\s*$/);
    if (match) return match[1];
  }
  return "";
}

function expandedLintProfileIds(profileIds, candidates) {
  const expanded = [...profileIds];
  if (expanded.includes("react-native") && !expanded.includes("android")) {
    if (candidates.some((candidate) => candidate === "android" || candidate.startsWith("android/"))) {
      expanded.push("android");
    }
  }
  return expanded;
}

function profileLintContext(profileId, loadProfile) {
  const profile = loadProfile(profileId);
  const architecture = isMapping(profile?.architecture) ? profile.architecture : null;
  const roles = Array.isArray(architecture?.roles) ? architecture.roles : [];
  return { roles, managedRoots: architectureManagedRoots(roles) };
}

function contextPathIsRelevant(relPath, context) {
  if (!context || context.roles.length === 0) return false;
  return isRootGradleConfig(relPath)
    || matchRole(relPath, context.roles) !== null
    || isManagedArchitecturePath(relPath, context.managedRoots);
}

function firstProfileWithRoles(profileIds, contexts) {
  return profileIds.find((profileId) => contexts.get(profileId)?.roles.length > 0) ?? "";
}

function architectureLintIsActive(root, architecture, candidates) {
  if (architecture.strict_when_roots_present !== true) return true;
  if (!Array.isArray(architecture.activation_roots)) return true;
  const roots = architecture.activation_roots
    .filter((item) => typeof item === "string" && item.replace(/^\/+|\/+$/g, ""))
    .map((item) => item.replace(/^\/+|\/+$/g, ""));
  if (roots.length === 0) return true;
  return candidates.some((candidate) => roots.some(
    (activationRoot) => candidate === activationRoot || candidate.startsWith(`${activationRoot}/`),
  )) || roots.some((activationRoot) => fs.existsSync(path.join(root, ...activationRoot.split("/"))));
}

function isTestFile(relPath) {
  const stem = path.posix.parse(relPath).name;
  return stem.endsWith("Test") || stem.endsWith("Tests") || stem.endsWith("Spec")
    || stem.endsWith("Specs") || TEST_NAME_RE.test(stem);
}

function patternSpecificity(pattern) {
  const parts = pattern.replace(/^\/+|\/+$/g, "").split("/").filter(Boolean);
  const staticCount = parts.filter((part) => !/^<[A-Za-z][A-Za-z0-9_-]*>$/.test(part)).length;
  return [parts.length, staticCount];
}

function compareSpecificity(left, right) {
  return left[0] - right[0] || left[1] - right[1];
}

function staticPrefixBeforePlaceholder(pattern) {
  const parts = [];
  for (const part of pattern.replace(/^\/+|\/+$/g, "").split("/")) {
    if (/^<[A-Za-z][A-Za-z0-9_-]*>$/.test(part)) break;
    if (part) parts.push(part);
  }
  return parts.join("/");
}

function architectureFamilyParent(root) {
  const parts = root.split("/");
  if (parts.length <= 1 || !CORE_FAMILY_SEGMENTS.has(parts.at(-1).toLowerCase())) return "";
  return parts.slice(0, -1).join("/");
}

function isManagedArchitecturePath(relPath, managedRoots) {
  return managedRoots.some((root) => relPath === root || relPath.startsWith(`${root}/`));
}

function validateGradleNamespace(root, relPath, match) {
  const suffix = match.role?.package_suffix;
  if (typeof suffix !== "string" || !suffix) return [];
  const buildFile = roleBuildFile(root, match.pattern, match.captures, relPath);
  if (!buildFile) return [];
  const namespace = namespaceFromGradle(buildFile);
  if (!namespace) return [];
  let expected = suffix;
  for (const [key, value] of Object.entries(match.captures)) {
    expected = expected.replaceAll(`<${key}>`, packageSegment(value));
  }
  return namespace.endsWith(expected)
    ? []
    : [finding(relPath, `namespace ${namespace} does not match role suffix ${expected}`)];
}

function validatePair(root, relPath, roles, match) {
  const pairId = match.role?.pair_with;
  if (typeof pairId !== "string" || !pairId || Object.keys(match.captures).length === 0) return [];
  const pairRole = roles.find((role) => isMapping(role) && role.id === pairId);
  if (!pairRole || !Array.isArray(pairRole.paths)) return [];
  for (const pattern of pairRole.paths) {
    if (typeof pattern !== "string") continue;
    const concrete = replacePlaceholders(pattern, match.captures);
    if (fs.existsSync(path.join(root, ...concrete.split("/")))) return [];
  }
  return [finding(relPath, `${match.role.id ?? "role"} requires paired role ${pairId}`)];
}

function validateDeclaredModules(root, relPath, role, captures) {
  const expected = expectedModules(role, captures);
  if (expected.length === 0) return [];
  const declared = declaredGradleModules(root);
  if (declared.size === 0) return [];
  return expected
    .filter((module) => !declared.has(module))
    .map((module) => finding(relPath, `Gradle module ${module} is not declared in settings`));
}

function validateGradleDependencies(root, _relPath, match) {
  const buildFile = roleBuildFile(root, match.pattern, match.captures, _relPath);
  if (!buildFile) return [];
  const dependencies = gradleProjectDependencies(buildFile);
  const roleId = String(match.role?.id ?? "");
  const buildRelative = path.relative(root, buildFile).split(path.sep).join("/");
  const findings = [];
  for (const module of forbiddenGradleDependencies(roleId, match.captures)) {
    if (dependencies.has(module) || [...dependencies].some((dependency) => dependency.startsWith(`${module}:`))) {
      findings.push(finding(buildRelative, `${roleId} has forbidden Gradle dependency ${module}`));
    }
  }
  for (const module of requiredGradleDependencies(roleId, match.captures)) {
    if (!dependencies.has(module)) findings.push(finding(buildRelative, `${roleId} must depend on ${module}`));
  }
  return findings;
}

function expectedModules(role, captures) {
  if (!Array.isArray(role?.modules)) return [];
  const expected = [];
  for (const module of role.modules) {
    if (typeof module !== "string") continue;
    const concrete = replacePlaceholders(module, captures);
    if (!concrete.includes("<") && !expected.includes(concrete)) expected.push(concrete);
  }
  return expected;
}

function declaredGradleModules(root) {
  const settings = ["settings.gradle.kts", "settings.gradle"]
    .map((name) => path.join(root, name))
    .find(isRegularFile);
  if (!settings) return new Set();
  const text = fs.readFileSync(settings, "utf8");
  const modules = new Set([...text.matchAll(/['\"](:[A-Za-z0-9_:-]+)['\"]/g)].map((match) => match[1]));
  for (const match of text.matchAll(/include\s+['\"](:[A-Za-z0-9_:-]+)['\"]/g)) modules.add(match[1]);
  return modules;
}

function roleBuildFile(root, pattern, captures, relPath = null) {
  if (relPath && isGradleBuildFile(relPath)) {
    const candidate = path.join(root, ...relPath.split("/"));
    if (isRegularFile(candidate)) return candidate;
  }
  const roleRoot = path.join(root, ...replacePlaceholders(pattern, captures).split("/"));
  return ["build.gradle.kts", "build.gradle"]
    .map((name) => path.join(roleRoot, name))
    .find(isRegularFile) ?? null;
}

function gradleProjectDependencies(file) {
  const text = fs.readFileSync(file, "utf8");
  const dependencies = new Set();
  for (const match of text.matchAll(/project\(\s*['\"](:[A-Za-z0-9_:-]+)['\"]\s*\)/g)) dependencies.add(match[1]);
  for (const match of text.matchAll(/project\s+['\"](:[A-Za-z0-9_:-]+)['\"]/g)) dependencies.add(match[1]);
  for (const module of typeSafeProjectDependencies(text)) dependencies.add(module);
  return dependencies;
}

function forbiddenGradleDependencies(roleId, captures) {
  if (roleId === "core-domain") return [":app", ":core:data", ":core:network", ":core:platform", ":core:navigation:impl", ":feature"];
  if (roleId === "core-data") return [":app", ":feature"];
  if (roleId === "feature-api") return [":app", ":core:data", `:feature:${captures.feature ?? ""}:presentation`];
  if (roleId === "feature-presentation") return [":app", ":core:data"];
  if (roleId === "navigation-api") return [":app", ":core:navigation:impl", ":feature"];
  return [];
}

function requiredGradleDependencies(roleId, captures) {
  if (roleId === "core-data" && captures.context) return [`:core:domain:${captures.context}`];
  if (roleId === "feature-presentation" && captures.feature) return [`:feature:${captures.feature}:api`];
  if (roleId === "navigation-impl") return [":core:navigation:api"];
  return [];
}

function replacePlaceholders(value, captures) {
  let concrete = value;
  for (const [key, replacement] of Object.entries(captures)) concrete = concrete.replaceAll(`<${key}>`, replacement);
  return concrete;
}

function isRootGradleConfig(relPath) {
  return new Set(["settings.gradle", "settings.gradle.kts", "build.gradle", "build.gradle.kts"]).has(relPath);
}

function isGradleBuildFile(relPath) {
  return new Set(["build.gradle", "build.gradle.kts"]).has(path.posix.basename(relPath));
}

function namespaceFromGradle(file) {
  const match = fs.readFileSync(file, "utf8").match(/\bnamespace\s*=?\s*['\"]([A-Za-z_][A-Za-z0-9_.]*)['\"]/);
  return match?.[1] ?? "";
}

function packageSegment(value) {
  return String(value).replaceAll("-", "_").replace(/[^A-Za-z0-9_]/g, "").toLowerCase();
}

function finding(filePath, message) {
  return { path: filePath, message };
}

function nonemptyLines(value) {
  return String(value ?? "").split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
}

function isRegularFile(candidate) {
  try {
    return fs.statSync(candidate).isFile();
  } catch {
    return false;
  }
}

function isMapping(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function compareCodePoints(left, right) {
  return left < right ? -1 : left > right ? 1 : 0;
}
