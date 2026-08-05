import fs from "node:fs";
import path from "node:path";
import { parseSimpleYaml, splitFrontmatter } from "./frontmatter.mjs";


export const COMMON_PROFILE_SKILLS = new Set([
  "agent-flow",
  "agent-flow-concise-output",
  "architecture-reviewer",
  "clean-architecture",
  "clean-architecture-core",
  "code-review",
  "codebase-design",
  "code-generation-discipline",
  "comment-authoring-discipline",
  "comment-checker",
  "ddd-architecture",
  "domain-modeling",
  "full-feature-workflow",
  "grilling",
  "grill-with-docs",
  "plan-reviewer",
  "product-brief",
  "push-watch",
  "resolving-merge-conflicts",
  "tdd",
  "to-prd",
]);

export const PROFILE_SKILLS = new Map([
  ["android", [
    "android-appshell-error-handling",
    "android-clean-architecture",
    "android-clean-presentation-architecture",
    "android-code-review",
    // 우리가 배포하는 Android skill이다. 이름이 upstream과 겹쳐 보인다는 이유로
    // profile 표가 이 둘을 외부 것으로 선언해 install 대상에서 빠져 있었다.
    "android-debugging",
    "android-module-creator",
    "android-guides",
    // 설치와 활성화는 다른 층이다. 여기 없으면 카탈로그에 아예 안 올라가서
    // frontmatter의 taskTerms/pathGlobs가 무슨 선언을 하든 죽는다. 설치는
    // profile이 넓게 하고, 활성화는 선언이 좁게 한다.
    "android-sdui-architecture",
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

// profile 정의의 정본은 설치 가능한 패키지 안이다. 루트 사본을 읽던 시절의
// 경로를 그대로 두면 파일이 없어도 `PROFILE_SKILLS` 맵으로 조용히 폴백해서,
// YAML이 정본이라는 계약이 아무 신호 없이 깨진다.
export function profileYamlPath(kitRoot, profile) {
  return path.join(kitRoot, "src", "agent_flow", "profiles", `${profile}.yaml`);
}


function validateProfiles(profiles, kitRoot) {
  for (const profile of profiles) {
    if (!PROFILE_SKILLS.has(profile) && !fs.existsSync(profileYamlPath(kitRoot, profile))) {
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
    const frontmatter = splitFrontmatter(fs.readFileSync(skillPath, "utf8"));
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
  const profilePath = profileYamlPath(kitRoot, profile);
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
