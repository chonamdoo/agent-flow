import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { parseSimpleYaml } from "./frontmatter.mjs";
import { packageDependencies } from "./profile-detection.mjs";
import { KIT_ROOT, resolveManagedPython } from "./installer-shared.mjs";


export const BUNDLED_HOST_SKILL_NAMES = new Set([
  "agent-flow",
  "agent-flow-diagnosing-bugs",
  "app-shell-error-contract",
  "android-appshell-error-handling",
  "comment-authoring-discipline",
  "comment-checker",
  "ios-app-shell-error-handling",
  "react-app-shell-error-handling",
  "react-native-app-shell-error-handling",
  "kotlin-backend-development-guide",
  "spring-boot-development-guide",
  "ktor-development-guide",
  "llm-tool-development",
  "react-hook-form-zod",
  "react-web-seo",
  "react-storybook",
  "react-scroll-restoration",
  "react-runtime-i18n",
  "ga4-ecommerce-events",
  "datadog-rum-sourcemaps",
  "nextjs-auth-session",
  "webview-json-rpc-bridge",
]);
// 선택과 무관하게 모든 프로젝트가 받는 공통 자산. 워크플로 운영과 코드 생성
// 규율이며, 구조 취향이 아니다.
export const COMMON_PROFILE_SKILLS = new Set([
  "agent-flow",
  "agent-flow-diagnosing-bugs",
  "agent-flow-concise-output",
  "architecture-reviewer",
  "code-generation-discipline",
  "comment-authoring-discipline",
  "comment-checker",
  "ddd-architecture",
  "full-feature-workflow",
  "grill-with-docs",
  "plan-reviewer",
  "product-brief",
  "push-watch",
  "resolving-merge-conflicts",
  "tdd",
  "to-prd",
]);
// Clean을 고른 프로젝트에만 설치하는 계층 규범. `ddd-architecture`는 여기에 두지
// 않는다 — 그것은 도메인 모델링이고, workflow의 DDD 단계가 선택과 무관하게 요구한다.
// 설치 목록과 런타임 required가 갈리면 영원히 지울 수 없는 degraded가 남는다.
export const CLEAN_ARCHITECTURE_SKILLS = new Set([
  "clean-architecture-core",
]);

// 선언이 없는 기존 설치는 종전 동작을 유지한다. 여기서 pending으로 읽으면 재설치
// 한 번으로 Clean 강제가 사라진다.
export function seedSkills(architectureMode) {
  const names = new Set(COMMON_PROFILE_SKILLS);
  if (architectureMode === undefined || architectureMode === null || architectureMode === "clean") {
    for (const skill of CLEAN_ARCHITECTURE_SKILLS) names.add(skill);
  }
  return names;
}


export function resolveInstallSelection({ args, detectedProfile, kitRoot, projectRoot = kitRoot, architectureMode = null, architecturePlan = null }) {
  const requestedProfiles = optionValues(args, "--profile")
    .flatMap(splitCsv)
    .filter(Boolean);
  const explicitSkills = optionValues(args, "--skills")
    .concat(optionValues(args, "--skill"))
    .flatMap(splitCsv)
    .filter(Boolean);
  const hasExplicitSelection = requestedProfiles.length > 0 || explicitSkills.length > 0;
  const autoProfile = !hasExplicitSelection
    && profileSkillsFromSource(kitRoot, detectedProfile, projectRoot).length > 0
    ? [detectedProfile]
    : [];
  const profiles = requestedProfiles.length > 0 ? requestedProfiles : autoProfile;
  validateProfiles(profiles, kitRoot);
  const selection = {
    filtered: profiles.length > 0 || explicitSkills.length > 0,
    explicitSelection: hasExplicitSelection,
    profiles,
    explicitSkills,
    architectureMode,
    architecturePlan,
  };
  return buildFilteredSelection(selection, kitRoot, projectRoot);
}

export function mergeInstallSelectionWithPrevious(selection, previousIndex, kitRoot, projectRoot = kitRoot) {
  const previousSelection = previousIndex?.selection || {};
  if (!selection?.explicitSelection && previousSelection.mode === "filtered") {
    const previousProfiles = knownProfiles(previousSelection.profiles || [], kitRoot);
    const previousExplicitSkills = (previousSelection.explicit_skills || [])
      .filter((name) => !selection.architecturePlan?.excluded_skills?.includes(name));
    if (previousProfiles.length > 0 || previousExplicitSkills.length > 0) {
      return buildFilteredSelection({
        ...selection,
        filtered: true,
        profiles: previousProfiles,
        explicitSkills: previousExplicitSkills,
      }, kitRoot, projectRoot);
    }
  }
  if (!selection?.skillNames) {
    return selection;
  }
  const profiles = new Set(selection.profiles || []);
  const explicitSkills = new Set(selection.explicitSkills || []);
  if (previousSelection.mode === "filtered") {
    for (const profile of knownProfiles(previousSelection.profiles || [], kitRoot)) {
      profiles.add(profile);
    }
    for (const skill of previousSelection.explicit_skills || []) {
      if (skill && !selection.architecturePlan?.excluded_skills?.includes(skill)) {
        explicitSkills.add(skill);
      }
    }
  }
  return buildFilteredSelection({
    ...selection,
    profiles: [...profiles],
    explicitSkills: [...explicitSkills],
  }, kitRoot, projectRoot);
}

function buildFilteredSelection(selection, kitRoot, projectRoot) {
  const profiles = new Set(selection.profiles || []);
  validateProfiles(profiles, kitRoot);
  const explicitSkills = new Set(selection.explicitSkills || []);
  const excluded = new Set(selection.architecturePlan?.excluded_skills || []);
  const metadataOptions = { kitRoot, projectRoot, architectureMode: selection.architectureMode, architectureContract: selection.architecturePlan?.contract };
  const names = seedSkills(selection.architectureMode ?? null);
  if (!selection.filtered) {
    for (const entry of fs.readdirSync(path.join(kitRoot, "skills"), { withFileTypes: true })) {
      if (entry.isDirectory()) names.add(entry.name);
    }
  }
  for (const profile of profiles) {
    for (const skill of profileSkillsFromSource(kitRoot, profile, projectRoot)) names.add(skill);
  }
  metadataOptions.metadataFor = loadSkillMetadata(metadataOptions, new Set([...names, ...explicitSkills]));
  for (const name of names) {
    if (excluded.has(name) || !skillAppliesToArchitecture(name, metadataOptions)) names.delete(name);
  }
  for (const skill of explicitSkills) {
    if (excluded.has(skill) || !skillAppliesToArchitecture(skill, metadataOptions)) {
      throw new Error(`architecture ${selection.architectureMode} conflicts with explicitly requested skill ${skill}`);
    }
    names.add(skill);
  }
  for (const base of [path.join(projectRoot, ".agent-flow/local-skills"), path.join(projectRoot, "skills")]) {
    if (base === path.join(kitRoot, "skills") || !fs.existsSync(base)) continue;
    for (const entry of fs.readdirSync(base, { withFileTypes: true })) {
      if (entry.isDirectory() && !excluded.has(entry.name) && fs.existsSync(path.join(base, entry.name, "SKILL.md"))
          && skillAppliesToArchitecture(entry.name, metadataOptions)) names.add(entry.name);
    }
  }
  expandDependencies(names, { ...metadataOptions, excluded });
  return {
    ...selection,
    profiles: [...profiles],
    explicitSkills: [...explicitSkills],
    skillNames: names,
    copyRootNames: new Set(names),
  };
}

export function addDependencies(names, { kitRoot = process.cwd(), projectRoot = kitRoot, architectureMode = null, architectureContract = null, excluded = new Set() } = {}) {
  if (!names.size) return;
  const options = { kitRoot, projectRoot, architectureMode, architectureContract, excluded };
  expandDependencies(names, { ...options, metadataFor: loadSkillMetadata(options, names) });
}

function expandDependencies(names, options) {
  const { architectureMode, excluded } = options;
  let changed = true;
  while (changed) {
    changed = false;
    for (const name of [...names]) {
      for (const dependency of skillDependencies(name, options)) {
        if (excluded.has(dependency) || !skillAppliesToArchitecture(dependency, options)) {
          throw new Error(`architecture ${architectureMode} conflicts with required dependency ${name} -> ${dependency}`);
        }
        if (!names.has(dependency)) {
          names.add(dependency);
          changed = true;
        }
      }
    }
  }
}

// Packaged profile YAML is the sole source for profile skill installation.
export function profileYamlPath(kitRoot, profile) {
  return path.join(kitRoot, "src", "agent_flow", "profiles", `${profile}.yaml`);
}

function readUtf8File(filePath, label) {
  try {
    return fs.readFileSync(filePath, "utf8");
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    throw new Error(`${label} unreadable: ${filePath}: ${detail}`);
  }
}


function knownProfiles(profiles, kitRoot) {
  return profiles.filter(
    (profile) => profile && fs.existsSync(profileYamlPath(kitRoot, profile)),
  );
}


function validateProfiles(profiles, kitRoot) {
  for (const profile of profiles) {
    if (!fs.existsSync(profileYamlPath(kitRoot, profile))) {
      throw new Error(`unknown profile: ${profile}`);
    }
  }
}

function skillDependencies(name, options) {
  const metadata = options.metadataFor(name);
  if (!metadata) return [];
  return new Set([
    ...(metadata.requires || []),
    ...(metadata.dependencies || []),
    ...(metadata.requires_by_architecture?.[options.architectureMode ?? "clean"] || []),
  ]);
}

function skillAppliesToArchitecture(name, options) {
  const metadata = options.metadataFor(name);
  return !metadata?.architecture_modes
    || metadata.architecture_modes.includes(options.architectureMode ?? "clean");
}

function loadSkillMetadata(options, initialNames) {
  const names = new Set(initialNames);
  const roots = new Set([
    path.join(options.projectRoot, ".agent-flow", "local-skills"),
    path.join(options.projectRoot, "skills"),
    path.join(options.kitRoot, "skills"),
  ]);
  for (const root of roots) {
    if (!fs.existsSync(root)) continue;
    for (const entry of fs.readdirSync(root, { withFileTypes: true })) names.add(entry.name);
  }
  if (options.architectureContract) {
    names.add(path.basename(path.dirname(options.architectureContract)));
  }
  const sources = new Map();
  const results = new Map();
  const documents = [];
  for (const name of names) {
    const source = skillMetadataPaths(name, options).find((candidate) => fs.existsSync(candidate));
    if (!source) continue;
    sources.set(name, source);
    if (results.has(source)) continue;
    try {
      documents.push({ source, text: readSkillMetadataSource(source) });
      results.set(source, null);
    } catch (error) {
      results.set(source, { error });
    }
  }
  for (const document of parseSkillMetadataBatch(documents)) {
    results.set(document.source, {
      metadata: document.metadata,
      error: document.error === null ? null
        : new Error(`invalid skill metadata: ${document.source}: ${document.error}`),
    });
  }
  return (name) => {
    const result = results.get(sources.get(name));
    if (result?.error) throw result.error;
    return result?.metadata ?? null;
  };
}

function readSkillMetadataSource(source) {
  let descriptor;
  try {
    descriptor = fs.openSync(source, fs.constants.O_RDONLY | fs.constants.O_NONBLOCK);
    if (!fs.fstatSync(descriptor).isFile()) throw new Error("expected a regular file");
    return fs.readFileSync(descriptor, "utf8");
  } catch (error) {
    throw new Error(`skill metadata unreadable: ${source}: ${error.message}`, { cause: error });
  } finally {
    if (descriptor !== undefined) fs.closeSync(descriptor);
  }
}

function parseSkillMetadataBatch(documents) {
  if (!documents.length) return [];
  const managed = resolveManagedPython();
  const script = [
    "import os, runpy, sys",
    'sys.path = [sys.argv[1]] + [entry for entry in sys.path if entry not in ("", ".", os.getcwd())]',
    'runpy.run_module("agent_flow.core.skill_metadata", run_name="__main__")',
  ].join("\n");
  const result = spawnSync(
    managed.python,
    [managed.flag, "-B", "-c", script, path.join(KIT_ROOT, "src")],
    {
      cwd: KIT_ROOT,
      encoding: "utf8",
      input: JSON.stringify({ schema_version: 1, documents }),
      timeout: 30_000,
      maxBuffer: 16 * 1024 * 1024,
    },
  );
  try {
    if (result.error || result.status !== 0) {
      throw new Error(result.stderr?.trim() || result.error?.message || `exit ${result.status}`);
    }
    const payload = JSON.parse(result.stdout);
    validateSkillMetadataPayload(payload, documents);
    return payload.documents;
  } catch (error) {
    throw new Error(`skill metadata parser failed: ${error.message}`, { cause: error });
  }
}

function validateSkillMetadataPayload(payload, documents) {
  const record = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
  const strings = (value) => Array.isArray(value) && value.every((item) => typeof item === "string");
  const metadata = (value) => record(value)
    && ["requires", "dependencies", "architecture_modes", "requires_docs"].every(
      (key) => !Object.hasOwn(value, key) || strings(value[key]),
    )
    && (!Object.hasOwn(value, "requires_by_architecture")
      || (record(value.requires_by_architecture) && Object.values(value.requires_by_architecture).every(strings)));
  if (!record(payload) || payload.schema_version !== 1 || !Array.isArray(payload.documents)
      || payload.documents.length !== documents.length
      || !payload.documents.every((item, index) => record(item)
        && item.source === documents[index].source
        && (item.error === null
          ? item.metadata === null || metadata(item.metadata)
          : typeof item.error === "string" && item.error.length > 0 && item.metadata === null))) {
    throw new Error("invalid skill metadata payload (expected ordered schema version 1 documents)");
  }
}

function skillMetadataPaths(name, { kitRoot, projectRoot, architectureContract }) {
  if (architectureContract && path.basename(path.dirname(architectureContract)) === name) {
    return [path.join(projectRoot, architectureContract)];
  }
  const paths = [];
  if (projectRoot) {
    paths.push(path.join(projectRoot, ".agent-flow", "local-skills", name, "SKILL.md"));
    paths.push(path.join(projectRoot, "skills", name, "SKILL.md"));
  }
  if (kitRoot && kitRoot !== projectRoot) {
    paths.push(path.join(kitRoot, "skills", name, "SKILL.md"));
  }
  return paths;
}

function profileSkillsFromSource(kitRoot, profile, projectRoot) {
  const profilePath = profileYamlPath(kitRoot, profile);
  const names = skillsFromProfileYaml(profilePath);
  if (!fs.existsSync(profilePath)) return names;
  const metadata = parseSimpleYaml(readUtf8File(profilePath, "profile YAML"));
  const capabilities = arrayValue(metadata.capabilities);
  if (!capabilities.length) return names;
  const dependencies = packageDependencies(projectRoot);
  for (const capability of capabilities) {
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(capability)) throw new Error(`invalid profile capability: ${capability}`);
    const capabilityPath = profileYamlPath(kitRoot, `_${capability}`);
    const declaration = parseSimpleYaml(readUtf8File(capabilityPath, "profile capability"));
    if (!arrayValue(declaration.requires_dependencies).every((name) => dependencies.has(name))
        || arrayValue(declaration.excludes_dependencies).some((name) => dependencies.has(name))) continue;
    names.push(...skillsFromProfileYaml(capabilityPath));
  }
  return [...new Set(names)];
}

function skillsFromProfileYaml(profilePath) {
  if (!fs.existsSync(profilePath)) {
    return [];
  }
  const text = readUtf8File(profilePath, "profile YAML");
  const values = [];
  let inSkills = false;
  let inInstall = false;
  for (const line of text.split(/\r?\n/)) {
    if (/^\S/.test(line)) {
      inSkills = line.trim() === "skills:";
      inInstall = false;
      continue;
    }
    if (!inSkills) {
      continue;
    }
    if (/^  install:\s*$/.test(line)) {
      inInstall = true;
      continue;
    }
    if (/^  \S/.test(line)) {
      inInstall = false;
    }
    if (inInstall) {
      const match = line.match(/^\s+-\s*([A-Za-z0-9._-]+)\s*$/);
      if (match) {
        values.push(match[1]);
      }
    }
  }
  return values;
}

function arrayValue(value) {
  return Array.isArray(value) ? value.map(String) : [];
}

function optionValues(args, name) {
  const values = [];
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === name && args[index + 1]) {
      values.push(args[index + 1]);
      index += 1;
      continue;
    }
    if (arg.startsWith(`${name}=`)) {
      values.push(arg.slice(name.length + 1));
    }
  }
  return values;
}

function splitCsv(value) {
  return String(value)
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}
