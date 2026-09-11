import fs from "node:fs";
import path from "node:path";
import { parseSimpleYaml, splitFrontmatter } from "./frontmatter.mjs";
import { packageDependencies } from "./profile-detection.mjs";


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
export const COMMON_PROFILE_SKILLS = new Set([
  "agent-flow",
  "agent-flow-diagnosing-bugs",
  "agent-flow-concise-output",
  "architecture-reviewer",
  "clean-architecture",
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


export function resolveInstallSelection({ args, detectedProfile, kitRoot, projectRoot = kitRoot }) {
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
  const filtered = profiles.length > 0 || explicitSkills.length > 0;
  if (!filtered) {
    return {
      filtered: false,
      explicitSelection: hasExplicitSelection,
      profiles: [],
      explicitSkills,
      skillNames: null,
      copyRootNames: null,
    };
  }
  const names = new Set(COMMON_PROFILE_SKILLS);
  for (const profile of profiles) {
    for (const skill of profileSkillsFromSource(kitRoot, profile, projectRoot)) {
      names.add(skill);
    }
  }
  for (const skill of explicitSkills) {
    names.add(skill);
  }
  addDependencies(names, { kitRoot, projectRoot });
  return {
    filtered: true,
    explicitSelection: hasExplicitSelection,
    profiles,
    explicitSkills,
    skillNames: names,
    copyRootNames: new Set(names),
  };
}

export function mergeInstallSelectionWithPrevious(selection, previousIndex, kitRoot, projectRoot = kitRoot) {
  const previousSelection = previousIndex?.selection || {};
  if (!selection?.explicitSelection && previousSelection.mode === "filtered") {
    const previousProfiles = knownProfiles(previousSelection.profiles || [], kitRoot);
    const previousExplicitSkills = previousSelection.explicit_skills || [];
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
      if (skill) {
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
  const names = new Set(COMMON_PROFILE_SKILLS);
  for (const profile of profiles) {
    for (const skill of profileSkillsFromSource(kitRoot, profile, projectRoot)) {
      names.add(skill);
    }
  }
  for (const skill of explicitSkills) {
    names.add(skill);
  }
  addDependencies(names, { kitRoot, projectRoot });
  return {
    ...selection,
    filtered: true,
    profiles: [...profiles],
    explicitSkills: [...explicitSkills],
    skillNames: names,
    copyRootNames: new Set(names),
  };
}

export function addDependencies(names, { kitRoot = process.cwd(), projectRoot = kitRoot } = {}) {
  let changed = true;
  while (changed) {
    changed = false;
    for (const name of [...names]) {
      for (const dependency of skillDependencies(name, { kitRoot, projectRoot })) {
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

function skillDependencies(name, { kitRoot, projectRoot }) {
  return metadataDependenciesForSkill(name, { kitRoot, projectRoot }).filter(isSafeSkillName);
}

function metadataDependenciesForSkill(name, { kitRoot, projectRoot }) {
  const dependencies = new Set();
  for (const skillPath of skillMetadataPaths(name, { kitRoot, projectRoot })) {
    if (!fs.existsSync(skillPath)) {
      continue;
    }
    const frontmatter = splitFrontmatter(readUtf8File(skillPath, "skill metadata"));
    if (!frontmatter) {
      continue;
    }
    const metadata = parseSimpleYaml(frontmatter);
    // `requires` is the bundled contract. `dependencies` remains readable for
    // project-local skills created by older agent-flow releases.
    for (const dependency of [
      ...arrayValue(metadata.requires),
      ...arrayValue(metadata.dependencies),
    ]) {
      dependencies.add(dependency);
    }
  }
  return [...dependencies];
}

function skillMetadataPaths(name, { kitRoot, projectRoot }) {
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

function isSafeSkillName(value) {
  return /^[A-Za-z0-9._-]+$/.test(String(value));
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
