import fs from "node:fs";
import path from "node:path";

export const SKILL_DEPENDENCIES = new Map([
  ["clean-architecture", ["clean-architecture-core"]],
  ["diagnosing-bugs", ["improve-codebase-architecture"]],
  ["grill-with-docs", ["domain-modeling", "grilling"]],
  ["improve-codebase-architecture", ["codebase-design", "domain-modeling", "grilling"]],
  ["setup-matt-pocock-skills", [
    "diagnosing-bugs",
    "domain-modeling",
    "grill-with-docs",
    "improve-codebase-architecture",
    "qa",
    "tdd",
    "to-issues",
    "to-prd",
    "triage",
  ]],
  ["tdd", ["codebase-design"]],
  ["to-issues", ["setup-matt-pocock-skills"]],
  ["to-prd", ["setup-matt-pocock-skills"]],
  ["triage", ["domain-modeling", "grilling", "setup-matt-pocock-skills"]],
  ["android-clean-architecture", ["clean-architecture-core"]],
  ["ios-clean-architecture", ["clean-architecture-core"]],
  ["react-clean-architecture", ["clean-architecture-core"]],
  ["react-native-clean-architecture", ["clean-architecture-core"]],
  ["python-api-clean-architecture", ["clean-architecture-core"]],
]);

export const COMMON_PROFILE_SKILLS = new Set([
  "agent-flow",
  "agent-flow-concise-output",
  "architecture-reviewer",
  "clean-architecture",
  "clean-architecture-core",
  "codebase-design",
  "code-generation-discipline",
  "comment-authoring-discipline",
  "comment-checker",
  "ddd-architecture",
  "ddd-clean-architecture",
  "diagnosing-bugs",
  "domain-modeling",
  "full-feature-workflow",
  "grilling",
  "grill-with-docs",
  "improve-codebase-architecture",
  "plan-reviewer",
  "product-brief",
  "push-watch",
  "qa",
  "setup-matt-pocock-skills",
  "tdd",
  "to-issues",
  "to-prd",
  "triage",
]);

export const PROFILE_SKILLS = new Map([
  ["android", [
    "android-appshell-error-handling",
    "android-clean-architecture",
    "android-clean-presentation-architecture",
    "android-code-review",
    "android-guides",
  ]],
  ["ios", [
    "ios-app-shell-error-handling",
    "ios-clean-architecture",
    "ios-clean-presentation-architecture",
  ]],
  ["nextjs", [
    "react-app-shell-error-handling",
    "react-clean-architecture",
    "react-clean-presentation-architecture",
    "react-development-guide",
    "typescript-development-guide",
  ]],
  ["python", [
    "python-api-clean-architecture",
    "python-development-guide",
  ]],
  ["react-native", [
    "react-native-app-shell-error-handling",
    "react-native-clean-architecture",
    "react-native-clean-presentation-architecture",
    "react-native-development-guide",
    "react-native-operational-adoption",
    "typescript-development-guide",
  ]],
  ["typescript", [
    "typescript-development-guide",
  ]],
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
  const autoProfile = !hasExplicitSelection && PROFILE_SKILLS.has(detectedProfile)
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
    for (const skill of profileSkillsFromSource(kitRoot, profile)) {
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
    const previousProfiles = previousSelection.profiles || [];
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
    for (const profile of previousSelection.profiles || []) {
      if (profile) {
        profiles.add(profile);
      }
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
  const explicitSkills = new Set(selection.explicitSkills || []);
  const names = new Set(COMMON_PROFILE_SKILLS);
  for (const profile of profiles) {
    for (const skill of profileSkillsFromSource(kitRoot, profile)) {
      names.add(skill);
    }
  }
  for (const skill of explicitSkills) {
    names.add(skill);
  }
  validateProfiles(profiles, kitRoot);
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

function validateProfiles(profiles, kitRoot) {
  for (const profile of profiles) {
    if (!PROFILE_SKILLS.has(profile) && !fs.existsSync(path.join(kitRoot, "profiles", `${profile}.yaml`))) {
      throw new Error(`unknown profile: ${profile}`);
    }
  }
}

function skillDependencies(name, { kitRoot, projectRoot }) {
  return [
    ...(SKILL_DEPENDENCIES.get(name) || []),
    ...metadataDependenciesForSkill(name, { kitRoot, projectRoot }),
  ].filter(isSafeSkillName);
}

function metadataDependenciesForSkill(name, { kitRoot, projectRoot }) {
  const dependencies = new Set();
  for (const skillPath of skillMetadataPaths(name, { kitRoot, projectRoot })) {
    if (!fs.existsSync(skillPath)) {
      continue;
    }
    const frontmatter = splitSkillFrontmatter(fs.readFileSync(skillPath, "utf8"));
    if (!frontmatter) {
      continue;
    }
    const metadata = parseSimpleYaml(frontmatter);
    for (const dependency of [
      ...arrayValue(metadata.dependencies),
      ...arrayValue(metadata.requires),
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

function profileSkillsFromSource(kitRoot, profile) {
  const profilePath = path.join(kitRoot, "profiles", `${profile}.yaml`);
  const fromYaml = skillsFromProfileYaml(profilePath);
  if (fromYaml.length > 0) {
    return fromYaml;
  }
  return PROFILE_SKILLS.get(profile) || [];
}

function skillsFromProfileYaml(profilePath) {
  if (!fs.existsSync(profilePath)) {
    return [];
  }
  const text = fs.readFileSync(profilePath, "utf8");
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

function splitSkillFrontmatter(text) {
  if (!text.startsWith("---\n")) {
    return null;
  }
  const end = text.indexOf("\n---\n", 4);
  return end === -1 ? null : text.slice(4, end);
}

function parseSimpleYaml(text) {
  const metadata = {};
  let listKey = null;
  for (const line of text.split(/\r?\n/)) {
    const listItem = line.match(/^\s+-\s*(.+)$/);
    if (listItem && listKey) {
      metadata[listKey].push(stripQuotes(listItem[1].trim()));
      continue;
    }
    const match = line.match(/^([A-Za-z0-9_-]+):\s*(.*)$/);
    if (!match) {
      listKey = null;
      continue;
    }
    const key = match[1];
    const raw = match[2].trim();
    if (raw.startsWith("[") && raw.endsWith("]")) {
      metadata[key] = raw.slice(1, -1).split(",").map((item) => stripQuotes(item.trim())).filter(Boolean);
      listKey = null;
    } else if (raw === "") {
      metadata[key] = [];
      listKey = key;
    } else {
      metadata[key] = stripQuotes(raw);
      listKey = null;
    }
  }
  return metadata;
}

function arrayValue(value) {
  return Array.isArray(value) ? value.map(String) : [];
}

function stripQuotes(value) {
  return value.replace(/^['"]|['"]$/g, "");
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
