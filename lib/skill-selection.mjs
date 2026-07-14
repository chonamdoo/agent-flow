import fs from "node:fs";
import crypto from "node:crypto";
import path from "node:path";

export const SKILL_DEPENDENCIES = new Map([
  ["clean-architecture", ["clean-architecture-core"]],
  ["diagnosing-bugs", ["improve-codebase-architecture"]],
  ["grill-with-docs", ["domain-modeling", "grilling"]],
  ["improve-codebase-architecture", ["codebase-design", "domain-modeling", "grilling"]],
  ["setup-matt-pocock-skills", [
    "code-review",
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
  ["tdd", ["codebase-design", "code-review"]],
  ["to-issues", ["setup-matt-pocock-skills"]],
  ["to-prd", ["setup-matt-pocock-skills"]],
  ["triage", ["domain-modeling", "grilling", "setup-matt-pocock-skills"]],
  ["android-clean-architecture", ["clean-architecture-core"]],
  ["ios-clean-architecture", ["clean-architecture-core"]],
  ["react-clean-architecture", ["clean-architecture-core"]],
  ["react-native-clean-architecture", ["clean-architecture-core"]],
  ["python-api-clean-architecture", ["clean-architecture-core"]],
]);

const GENERIC_FILE_RULE_SCORE = 20;
const REPLACEABLE_SNAPSHOT_SOURCES = new Set(["bundled", "host-bootstrap", "shared"]);
export const EXPLICIT_ONLY_SKILLS = new Set(["testing-localization"]);

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
  "to-spec",
  "to-tickets",
  "triage",
]);

export const PROFILE_SKILLS = new Map([
  ["android", [
    "android-appshell-error-handling",
    "android-clean-architecture",
    "android-clean-presentation-architecture",
    "android-code-review",
    "android-guides",
    "android-intent-security",
    "android-debugging",
    "android-module-creator",
    "adaptive",
    "agp-9-upgrade",
    "android-cli",
    "camera1-to-camerax",
    "compose-animations",
    "compose-focus-navigation",
    "compose-modifier-and-layout-style",
    "compose-recomposition-performance",
    "compose-side-effects",
    "compose-slot-api-pattern",
    "compose-stability-diagnostics",
    "compose-state-authoring",
    "compose-state-deferred-reads",
    "compose-state-hoisting",
    "compose-state-holder-ui-split",
    "compose-ui-testing-patterns",
    "display-glasses-with-jetpack-compose-glimmer",
    "edge-to-edge",
    "engage-sdk-integration",
    "jetpack-compose-m3",
    "kotlin-coroutines-structured-concurrency",
    "kotlin-flow-state-event-modeling",
    "kotlin-multiplatform-expect-actual",
    "kotlin-types-value-class",
    "migrate-xml-views-to-jetpack-compose",
    "navigation-3",
    "perfetto-sql",
    "perfetto-trace-analysis",
    "play-billing-library-version-upgrade",
    "play-policy-insights",
    "r8-analyzer",
    "styles",
    "testing-localization",
    "testing-setup",
    "verified-email",
    "appfunctions",
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
  ["node", [
    "node-development-guide",
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
  ["spring", [
    "spring-development-guide",
  ]],
  ["typescript", [
    "typescript-development-guide",
  ]],
]);

export function resolveInstallSelection({ args, detectedProfile, kitRoot, projectRoot = kitRoot }) {
  const requestedProfileValues = optionValues(args, "--profile")
    .flatMap(splitCsv)
    .filter(Boolean);
  const explicitSkillValues = optionValues(args, "--skills")
    .concat(optionValues(args, "--skill"))
    .flatMap(splitCsv)
    .filter(Boolean);
  validateProfileNames(requestedProfileValues);
  validateSafeNames(explicitSkillValues, "skill");
  const requestedProfiles = uniqueLogicalNames(requestedProfileValues);
  const explicitSkills = uniqueLogicalNames(explicitSkillValues);
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
      explicitProfileSelection: requestedProfiles.length > 0,
      profileSelection: requestedProfiles.length > 0 ? "explicit" : "auto",
      profiles: [],
      explicitSkills,
      skillNames: null,
      copyRootNames: null,
    };
  }
  const names = new LogicalNameSet(COMMON_PROFILE_SKILLS);
  const profileRouting = loadProfileRouting(kitRoot);
  const skillProfiles = installationProfiles(profiles, projectRoot, profileRouting);
  const requirementProfiles = profileRequirementProfiles(profiles, skillProfiles, profileRouting);
  for (const profile of skillProfiles) {
    for (const skill of profileSkillsFromSource(kitRoot, profile)) {
      names.add(skill);
    }
  }
  for (const skill of explicitSkills) {
    names.add(skill);
  }
  addProjectSkillNames(names, projectRoot, kitRoot);
  addDependencies(names, { kitRoot, projectRoot });
  applyExplicitOnlyPolicy(names, explicitSkills);
  return {
    filtered: true,
    explicitSelection: hasExplicitSelection,
    explicitProfileSelection: requestedProfiles.length > 0,
    profileSelection: requestedProfiles.length > 0 ? "explicit" : "auto",
    profiles,
    skillProfiles,
    explicitSkills,
    requiredReview: requiredReviewSkills(requirementProfiles, kitRoot),
    conditionalSkills: conditionalProfileSkills(requirementProfiles, kitRoot),
    profileRouting,
    skillNames: names,
    copyRootNames: new LogicalNameSet(names),
  };
}

export function mergeInstallSelectionWithPrevious(selection, previousIndex, kitRoot, projectRoot = kitRoot) {
  const previousSelection = previousIndex?.selection || {};
  if (!selection?.explicitSelection && previousSelection.mode === "filtered") {
    const previousProfiles = uniqueLogicalNames(previousSelection.profiles || []);
    const previousExplicitSkills = previousMergedExplicitSkills(previousSelection.explicit_skills || []);
    if (previousProfiles.length > 0 || previousExplicitSkills.size > 0) {
      return buildFilteredSelection({
        ...selection,
        filtered: true,
        profileSelection: previousSelection.profile_selection || "explicit",
        profiles: previousProfiles,
        explicitSkills: [...previousExplicitSkills],
      }, kitRoot, projectRoot);
    }
  }
  if (!selection?.skillNames) {
    return selection;
  }
  const profiles = new LogicalNameSet(selection.profiles || []);
  const explicitSkills = new LogicalNameSet(selection.explicitSkills || []);
  if (previousSelection.mode === "filtered") {
    for (const profile of previousSelection.profiles || []) {
      if (profile) {
        profiles.add(profile);
      }
    }
    for (const skill of previousSelection.explicit_skills || []) {
      if (skill && !EXPLICIT_ONLY_SKILLS.has(portableSkillCasefold(skill))) {
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
  const profiles = new LogicalNameSet(selection.profiles || []);
  const explicitSkills = new LogicalNameSet(selection.explicitSkills || []);
  const names = new LogicalNameSet(COMMON_PROFILE_SKILLS);
  const profileRouting = loadProfileRouting(kitRoot);
  const skillProfiles = installationProfiles(profiles, projectRoot, profileRouting);
  const requirementProfiles = profileRequirementProfiles(profiles, skillProfiles, profileRouting);
  for (const profile of skillProfiles) {
    for (const skill of profileSkillsFromSource(kitRoot, profile)) {
      names.add(skill);
    }
  }
  for (const skill of explicitSkills) {
    names.add(skill);
  }
  validateProfiles(profiles, kitRoot);
  addProjectSkillNames(names, projectRoot, kitRoot);
  addDependencies(names, { kitRoot, projectRoot });
  applyExplicitOnlyPolicy(names, explicitSkills);
  return {
    ...selection,
    filtered: true,
    profiles: [...profiles],
    skillProfiles,
    explicitSkills: [...explicitSkills],
    requiredReview: requiredReviewSkills(requirementProfiles, kitRoot),
    conditionalSkills: conditionalProfileSkills(requirementProfiles, kitRoot),
    profileRouting,
    skillNames: names,
    copyRootNames: new LogicalNameSet(names),
  };
}

function previousMergedExplicitSkills(values) {
  return new LogicalNameSet(
    values.filter((name) => !EXPLICIT_ONLY_SKILLS.has(portableSkillCasefold(name))),
  );
}

function applyExplicitOnlyPolicy(names, explicitSkills) {
  const explicit = explicitSkills instanceof Set ? explicitSkills : new LogicalNameSet(explicitSkills || []);
  for (const name of EXPLICIT_ONLY_SKILLS) {
    if (!explicit.has(name)) names.delete(name);
  }
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

export function resolveProfileSkillSources({
  skillNames,
  kitRoot,
  projectRoot,
  projectSkillsRoot,
  home,
  activeHost = null,
  env = {},
  previousIndex = null,
  automaticSkillNames = [],
  explicitSkillNames = [],
  policy = loadSkillSourcePolicy(path.join(kitRoot, "skills", "source-policy.yaml")),
}) {
  validateLockedSkillTrees(kitRoot);
  if (!skillNames) {
    return { entries: [], missing: [] };
  }
  const hostRoots = profileSkillHostRoots(home, env);
  const resolvedProjectRoot = projectRoot ?? path.dirname(path.dirname(projectSkillsRoot));
  const entries = [];
  const missing = [];
  const previousByName = indexSkillsByLogicalName(previousIndex?.skills || [], "previous skill index");
  const pendingNames = new LogicalNameSet(skillNames);
  const automaticRoots = new LogicalNameSet(automaticSkillNames);
  const explicitRoots = new LogicalNameSet(explicitSkillNames);
  for (const name of EXPLICIT_ONLY_SKILLS) {
    if (!explicitRoots.has(name)) pendingNames.delete(name);
  }
  const regularRoots = new LogicalNameSet(
    [...pendingNames].filter((name) => !automaticRoots.has(name) || explicitRoots.has(name)),
  );
  const resolvedNames = new Set();
  while (true) {
    const name = [...pendingNames]
      .filter((candidate) => !resolvedNames.has(portableSkillCasefold(candidate)))
      .sort(compareCodePoints)[0];
    if (name === undefined) break;
    const logicalName = portableSkillCasefold(name);
    resolvedNames.add(logicalName);
    if (!isSafeSkillName(name)) {
      throw new Error(`unsafe skill name: ${name}`);
    }
    const candidates = new Map([
      ["project-local", { kind: "local", root: projectSkillRoot(path.join(resolvedProjectRoot, ".agent-flow", "local-skills"), name, resolvedProjectRoot), host: null, authority_root: resolvedProjectRoot }],
      ["project", sameDirectory(resolvedProjectRoot, kitRoot)
        ? null
        : { kind: "project", root: projectSkillRoot(path.join(resolvedProjectRoot, "skills"), name, resolvedProjectRoot), host: null, authority_root: resolvedProjectRoot }],
      ["bundled", { kind: "bundled", root: projectSkillRoot(path.join(kitRoot, "skills"), name, kitRoot), host: null, authority_root: kitRoot }],
      ["project-snapshot", { kind: "project-snapshot", root: projectSkillRoot(projectSkillsRoot, name, resolvedProjectRoot), host: null, authority_root: resolvedProjectRoot }],
      ["shared", { kind: "shared", root: externalSkillRoot(path.join(home, ".agents", "skills"), name), host: null, authority_root: home }],
    ]);
    let selected;
    const previous = previousByName.get(logicalName);
    for (const source of ["project-local", "project"]) {
      const candidate = candidates.get(source);
      if (!candidate) continue;
      if (!fs.existsSync(path.join(candidate.root, "SKILL.md"))) continue;
      assertSkillSourceProvenance(candidate.authority_root, candidate.root);
      assertSelectedSkillLogicalName(candidate.root, name);
      selected = { ...candidate, tree_hash: hashSkillTree(candidate.root) };
      break;
    }

    const rejectedExternal = [];
    const snapshot = candidates.get("project-snapshot");
    const snapshotExists = fs.existsSync(path.join(snapshot.root, "SKILL.md"));
    const pinnedSourceHost = previous?.source === "host-bootstrap" && !snapshotExists
      ? previous.source_host
      : null;
    const pinnedTreeHash = pinnedSourceHost === null ? null : previous?.tree_hash;
    if (
      !selected
      && previous?.source === "host-bootstrap"
      && !snapshotExists
      && policy.rules.snapshot_replace_from_other_host === "deny"
      && (
        typeof pinnedSourceHost !== "string"
        || !hostRoots.some((candidate) => candidate.host === pinnedSourceHost)
        || typeof pinnedTreeHash !== "string"
        || !/^[0-9a-f]{64}$/.test(pinnedTreeHash)
      )
    ) {
      throw new Error(`invalid pinned host skill provenance: ${name}`);
    }
    let untrustedSnapshotHash = null;
    if (!previous && snapshotExists) {
      assertSkillSourceProvenance(snapshot.authority_root, snapshot.root);
      untrustedSnapshotHash = hashSkillTree(snapshot.root);
    }
    if (!selected && previous && snapshotExists) {
      assertSkillSourceProvenance(snapshot.authority_root, snapshot.root);
      const snapshotHash = hashSkillTree(snapshot.root);
      if (previous?.tree_hash && previous.tree_hash !== snapshotHash) {
        throw new Error(`existing skill snapshot changed: ${name}`);
      }
      const managedSnapshotIsUnchanged = previous?.tree_hash === snapshotHash
        && REPLACEABLE_SNAPSHOT_SOURCES.has(previous?.source);
      const automaticOnly = automaticRoots.has(name) && !regularRoots.has(name);
      if (automaticOnly) {
        const normalizedActiveHost = hostRoots.some((candidate) => candidate.host === activeHost)
          ? activeHost
          : null;
        const activeRoot = hostRoots.find((candidate) => candidate.host === normalizedActiveHost);
        const currentCandidates = [
          activeRoot
            ? {
                kind: "host-bootstrap",
                root: externalSkillRoot(activeRoot.root, name),
                host: normalizedActiveHost,
                authority_root: activeRoot.authorityRoot,
              }
            : null,
          candidates.get("shared"),
          candidates.get("bundled"),
        ].filter(Boolean);
        for (const candidate of currentCandidates) {
          selected = inspectExternalSkillCandidate(candidate, name, rejectedExternal);
          if (selected) {
            selected.replace_existing = selected.tree_hash !== snapshotHash
              || selected.kind !== previous.source
              || (selected.host ?? null) !== (previous.source_host ?? null);
            break;
          }
        }
        if (!selected) continue;
      } else if (["host-bootstrap", "shared"].includes(previous?.source)) {
        const original = authenticatedPreviousExternalSource({
          candidates,
          hostRoots,
          name,
          previous,
          rejected: rejectedExternal,
        });
        const explicitSource = explicitRoots.has(name);
        const rejectedDetail = rejectedExternal[rejectedExternal.length - 1] || "source is unavailable";
        const sourceFailure = !original
          ? explicitSource
            ? `explicit skill ${name} rejected pinned source: ${rejectedDetail}`
            : `pinned external skill source is unavailable: ${name}; ${rejectedDetail}`
          : original.tree_hash !== previous.tree_hash
            ? explicitSource
              ? `explicit skill ${name} rejected changed source ${original.root}: tree hash changed`
              : `pinned external skill source changed: ${name}`
            : null;
        if (sourceFailure) {
          throw new Error(sourceFailure);
        }
      }
      if (!automaticOnly && previous?.source === "bundled" && managedSnapshotIsUnchanged) {
        const bundled = inspectExternalSkillCandidate(candidates.get("bundled"), name, rejectedExternal);
        if (bundled && bundled.tree_hash !== snapshotHash) {
          selected = { ...bundled, replace_existing: true };
        }
      }
      selected ??= { ...snapshot, tree_hash: snapshotHash };
    }

    if (!selected && pinnedSourceHost !== null) {
      const pinnedRoot = hostRoots.find((candidate) => candidate.host === pinnedSourceHost);
      const pinnedCandidate = inspectExternalSkillCandidate({
        kind: "host-bootstrap",
        root: externalSkillRoot(pinnedRoot.root, name),
        host: pinnedSourceHost,
        authority_root: pinnedRoot.authorityRoot,
      }, name, rejectedExternal);
      if (pinnedCandidate && pinnedCandidate.tree_hash !== pinnedTreeHash) {
        throw new Error(`pinned host skill snapshot changed: ${name}`);
      }
      if (!pinnedCandidate) {
        const deniedReplacementHosts = hostRoots
          .filter((candidate) => candidate.host !== pinnedSourceHost)
          .filter((candidate) => fs.existsSync(path.join(externalSkillRoot(candidate.root, name), "SKILL.md")))
          .map((candidate) => candidate.host)
          .sort(compareCodePoints);
        if (deniedReplacementHosts.length > 0) {
          throw new Error(
            `skill snapshot replacement from another host is denied: ${name} `
            + `(pinned ${pinnedSourceHost}; candidates ${deniedReplacementHosts.join(", ")})`,
          );
        }
        const fallbackExists = [candidates.get("shared"), candidates.get("bundled")]
          .some((candidate) => fs.existsSync(path.join(candidate.root, "SKILL.md")));
        const detail = rejectedExternal.length > 0
          ? `; rejected original source: ${rejectedExternal.join("; ")}`
          : "";
        throw new Error(
          `pinned host skill snapshot recovery requires the original source: ${name}`
          + `${fallbackExists ? "; shared or bundled replacement is denied" : ""}${detail}`,
        );
      }
      selected = pinnedCandidate;
    }

    if (!selected) {
      const normalizedActiveHost = hostRoots.some((candidate) => candidate.host === activeHost)
        ? activeHost
        : null;
      const activeRoot = hostRoots.find((candidate) => candidate.host === normalizedActiveHost);
      const externalCandidates = [
        activeRoot
          ? {
              kind: "host-bootstrap",
              root: externalSkillRoot(activeRoot.root, name),
              host: normalizedActiveHost,
              authority_root: activeRoot.authorityRoot,
            }
          : null,
        candidates.get("shared"),
        candidates.get("bundled"),
      ].filter(Boolean);
      for (const candidate of externalCandidates) {
        const rejectionCount = rejectedExternal.length;
        selected = inspectExternalSkillCandidate(candidate, name, rejectedExternal);
        if (
          !selected
          && candidate === externalCandidates[0]
          && candidate.kind === "host-bootstrap"
          && explicitRoots.has(name)
          && rejectedExternal.length > rejectionCount
        ) {
          throw new Error(
            `explicit skill ${name} rejected source ${candidate.root}: `
            + rejectedExternal[rejectedExternal.length - 1],
          );
        }
        if (selected) break;
      }
    }
    if (!selected) {
      const otherHosts = hostRoots
        .filter((candidate) => candidate.host !== activeHost)
        .map((candidate) => ({
          kind: "host-bootstrap",
          root: externalSkillRoot(candidate.root, name),
          host: candidate.host,
          authority_root: candidate.authorityRoot,
        }))
        .sort((left, right) => compareCodePoints(left.host, right.host));
      const hostCandidates = otherHosts
        .map((candidate) => inspectExternalSkillCandidate(candidate, name, rejectedExternal))
        .filter(Boolean);
      const hashes = new Set(hostCandidates.map((candidate) => candidate.tree_hash));
      if (hashes.size > 1) {
        const conflicts = hostCandidates
          .map((candidate) => `${candidate.host}:${candidate.root}:${candidate.tree_hash}`)
          .join("; ");
        throw new Error(
          `conflicting host skill snapshots for ${name}; rejected candidates: ${[
            ...rejectedExternal,
            conflicts,
          ].join("; ")}`,
        );
      }
      selected = hostCandidates[0];
    }
    if (!selected) {
      if (untrustedSnapshotHash !== null) {
        throw new Error(`untrusted existing skill snapshot has no authenticated source: ${name}`);
      }
      if (rejectedExternal.length > 0) {
        throw new Error(
          `no valid skill source for ${name}; rejected candidates: ${rejectedExternal.join("; ")}`,
        );
      }
      missing.push(name);
      continue;
    }
    const selectedHash = selected.tree_hash ?? hashSkillTree(selected.root);
    if (untrustedSnapshotHash !== null && untrustedSnapshotHash !== selectedHash) {
      throw new Error(`untrusted existing skill snapshot differs: ${name}`);
    }
    if (
      selected.kind === "project-snapshot"
      && previous?.tree_hash
      && previous.tree_hash !== selectedHash
    ) {
      throw new Error(`existing skill snapshot changed: ${name}`);
    }
    const preservedOrigin = selected.kind === "project-snapshot" && previous?.source && previous.tree_hash === selectedHash;
    entries.push({
      name: logicalName,
      source_kind: preservedOrigin ? previous.source : selected.kind,
      source_host: preservedOrigin ? (previous.source_host ?? null) : selected.host,
      source_path: selected.root,
      tree_hash: selectedHash,
      replace_existing: selected.replace_existing === true,
    });
    for (const dependency of selectedSkillDependencies(logicalName, selected.root)) {
      if (!EXPLICIT_ONLY_SKILLS.has(dependency) || explicitRoots.has(dependency)) {
        pendingNames.add(dependency);
      }
    }
  }
  const automaticClosure = resolvedSkillDependencyClosure(automaticRoots, entries);
  const regularClosure = resolvedSkillDependencyClosure(regularRoots, entries);
  for (const entry of entries) {
    if (
      automaticClosure.has(entry.name)
      && !regularClosure.has(entry.name)
      && ["host-bootstrap", "shared"].includes(entry.source_kind)
    ) {
      entry.automatic_on_demand = true;
    }
  }
  entries.sort((left, right) => compareCodePoints(left.name, right.name));
  missing.sort(compareCodePoints);
  return { entries, missing, skillNames: new LogicalNameSet([...entries.map((entry) => entry.name), ...missing]) };
}

export function discoverAutomaticExternalSkillNames({
  home,
  activeHost = null,
  env = {},
  previousIndex = null,
}) {
  const names = new LogicalNameSet();
  if (env.AGENT_FLOW_AUTO_EXTERNAL_SKILLS === "0") return names;
  const hostRoots = profileSkillHostRoots(home, env);
  const activeRoot = hostRoots.find((candidate) => candidate.host === activeHost);
  const roots = [
    ...(activeRoot ? [{ ...activeRoot, kind: "host-bootstrap" }] : []),
    {
      kind: "shared",
      root: path.join(path.resolve(home || "."), ".agents", "skills"),
      authorityRoot: path.resolve(home || "."),
      host: null,
    },
  ];
  for (const source of roots) {
    for (const name of validatedExternalSkillCatalog(source)) {
      if (!EXPLICIT_ONLY_SKILLS.has(name)) names.add(name);
    }
  }
  const previousExposure = previousIndex?.selection?.external_exposure_skills;
  if (previousExposure !== undefined) {
    if (!Array.isArray(previousExposure)) {
      throw new Error("invalid previous external skill exposure list");
    }
    for (const name of previousExposure) {
      if (!isSafeSkillName(name)) {
        throw new Error(`invalid previous external skill exposure name: ${name}`);
      }
      names.add(name);
    }
  }
  return names;
}

export function mergeResolvedSkillClosure(selection, sourcePlan) {
  if (!selection?.skillNames) return selection;
  const skillNames = new LogicalNameSet(selection.skillNames);
  for (const entry of sourcePlan?.entries || []) skillNames.add(entry.name);
  for (const name of sourcePlan?.missing || []) skillNames.add(name);
  applyExplicitOnlyPolicy(skillNames, selection.explicitSkills || []);
  return {
    ...selection,
    skillNames,
    copyRootNames: new LogicalNameSet(skillNames),
  };
}

function profileSkillHostRoots(home, env) {
  const resolvedHome = path.resolve(home || ".");
  const configuredCodexRoot = configuredHostRoot(env.CODEX_HOME, resolvedHome);
  const configuredClaudeRoot = configuredHostRoot(env.CLAUDE_CONFIG_DIR, resolvedHome);
  const configuredOmpRoot = configuredHostRoot(env.PI_CODING_AGENT_DIR, resolvedHome);
  return [
    hostSkillCandidate(
      "codex",
      configuredCodexRoot ?? path.join(resolvedHome, ".codex"),
      configuredCodexRoot ? path.dirname(configuredCodexRoot) : resolvedHome,
    ),
    hostSkillCandidate(
      "claude",
      configuredClaudeRoot ?? path.join(resolvedHome, ".claude"),
      configuredClaudeRoot ? path.dirname(configuredClaudeRoot) : resolvedHome,
    ),
    hostSkillCandidate(
      "omp",
      configuredOmpRoot ?? path.join(resolvedHome, ".omp", "agent"),
      configuredOmpRoot ? path.dirname(configuredOmpRoot) : resolvedHome,
    ),
  ];
}

function configuredHostRoot(value, home) {
  if (typeof value !== "string" || !value.trim()) return null;
  const configured = value.trim();
  if (configured === "~") return home;
  if (configured.startsWith("~/") || configured.startsWith("~\\")) {
    return path.resolve(home, configured.slice(2));
  }
  return path.resolve(configured);
}

function hostSkillCandidate(host, configRoot, authorityRoot) {
  return {
    host,
    root: path.join(configRoot, "skills"),
    authorityRoot,
  };
}

export function loadSkillSourcePolicy(policyPath) {
  if (!fs.existsSync(policyPath)) {
    throw new Error(`missing skill source policy: ${policyPath}`);
  }
  const text = fs.readFileSync(policyPath, "utf8");
  const authorityOrder = yamlListSection(text, "authority_order");
  const rules = yamlMapSection(text, "rules");
  const supported = new Set([
    "project-local",
    "project",
    "bundled",
    "project-snapshot",
    "active-host",
    "shared",
    "deterministic-host-bootstrap",
  ]);
  const requiredAuthorityOrder = [
    "project-local",
    "project",
    "project-snapshot",
    "active-host",
    "shared",
    "bundled",
    "deterministic-host-bootstrap",
  ];
  if (
    authorityOrder.some((source) => !supported.has(source))
    || authorityOrder.join("\0") !== requiredAuthorityOrder.join("\0")
  ) {
    throw new Error("invalid skill source authority_order");
  }
  if (
    rules.missing_required_skill !== "fail"
    || rules.source_symlink !== "fail"
    || rules.host_hash_conflict !== "fail"
    || rules.snapshot_replace_from_other_host !== "deny"
    || rules.runtime_global_lookup !== "deny"
    || rules.automatic_skill_mutation !== "deny"
    || rules.automatic_invalid_candidate !== "fallback-with-diagnostic"
    || rules.hash_scope !== "whole-tree"
  ) {
    throw new Error("invalid skill source runtime/hash policy");
  }
  return { authorityOrder, rules };
}

export function validateLockedSkillTrees(kitRoot) {
  const lockPath = path.join(kitRoot, "skills", "upstream-lock.json");
  if (!fs.existsSync(lockPath)) return;
  let lock;
  try {
    lock = JSON.parse(fs.readFileSync(lockPath, "utf8"));
  } catch (error) {
    throw new Error(`invalid upstream skill lock: ${error instanceof Error ? error.message : String(error)}`);
  }
  const exactCopies = lock?.exact_copies;
  const localAdaptations = lock?.local_adaptations;
  const projectTreeHashes = lock?.project_tree_hashes;
  if (
    lock?.whole_tree_required !== true
    || !exactCopies
    || typeof exactCopies !== "object"
    || Array.isArray(exactCopies)
    || !localAdaptations
    || typeof localAdaptations !== "object"
    || Array.isArray(localAdaptations)
    || !projectTreeHashes
    || typeof projectTreeHashes !== "object"
    || Array.isArray(projectTreeHashes)
  ) {
    throw new Error("invalid upstream skill whole-tree lock");
  }
  const exactNames = Object.keys(exactCopies);
  const adaptedNames = Object.keys(localAdaptations);
  if (exactNames.some((name) => adaptedNames.includes(name))) {
    throw new Error("invalid upstream skill lock source classification");
  }
  const lockedNames = [...exactNames, ...adaptedNames].sort(compareCodePoints);
  const hashNames = Object.keys(projectTreeHashes).sort(compareCodePoints);
  if (JSON.stringify(lockedNames) !== JSON.stringify(hashNames)) {
    throw new Error("invalid upstream skill project tree hash coverage");
  }
  for (const name of lockedNames) {
    if (!isSafeSkillName(name) || !/^[0-9a-f]{64}$/.test(projectTreeHashes[name])) {
      throw new Error(`invalid upstream skill project tree hash: ${name}`);
    }
    const skillRoot = path.join(kitRoot, "skills", name);
    if (!fs.existsSync(path.join(skillRoot, "SKILL.md"))) {
      throw new Error(`missing locked project skill snapshot: ${name}`);
    }
    assertSkillSourceProvenance(kitRoot, skillRoot);
    if (hashSkillTree(skillRoot) !== projectTreeHashes[name]) {
      throw new Error(`locked project skill snapshot changed: ${name}`);
    }
  }
  validateAndroidOfficialLock(kitRoot, lock.android_official);
}

export function validateAndroidOfficialLock(kitRoot, official) {
  if (official === undefined) return;
  const snapshots = official?.snapshots;
  if (
    official?.source !== "https://github.com/android/skills"
    || !/^[0-9a-f]{40}$/.test(official?.commit || "")
    || official?.runtime_fetch !== false
    || official?.catalog !== "profiles/android.yaml#android_skills.implementation"
    || official?.runtime_tree_verification !== "installed-index"
    || !snapshots
    || typeof snapshots !== "object"
    || Array.isArray(snapshots)
  ) {
    throw new Error("invalid Android official skill lock");
  }
  const sourcePolicyPath = path.join(kitRoot, "skills", "source-policy.yaml");
  const officialPolicy = fs.existsSync(sourcePolicyPath)
    ? yamlMapSection(fs.readFileSync(sourcePolicyPath, "utf8"), "official_project_snapshots")
    : {};
  if (
    officialPolicy.source !== official.source
    || officialPolicy.commit !== official.commit
    || officialPolicy.catalog !== official.catalog
    || officialPolicy.install_policy !== official.policy
    || officialPolicy.runtime_fetch !== "false"
    || officialPolicy.offline_validation !== "required"
    || officialPolicy.runtime_tree_verification !== official.runtime_tree_verification
  ) {
    throw new Error("Android official source policy does not match lock provenance");
  }
  const profilePath = path.join(kitRoot, "profiles", "android.yaml");
  if (!fs.existsSync(profilePath)) throw new Error("missing Android profile for official skill lock");
  const catalogs = skillCatalogsFromProfileYaml(fs.readFileSync(profilePath, "utf8"));
  const catalogNames = uniqueLogicalNames(catalogs.android_skills?.implementation || [])
    .sort(compareCodePoints);
  const snapshotNames = Object.keys(snapshots).map(portableSkillCasefold).sort(compareCodePoints);
  if (JSON.stringify(catalogNames) !== JSON.stringify(snapshotNames)) {
    throw new Error("Android official skill catalog does not match lock coverage");
  }
  const licenseReference = String(official.license_reference || "");
  if (!isSafeRelativePath(licenseReference) || !/^[0-9a-f]{64}$/.test(official.license_sha256 || "")) {
    throw new Error("invalid Android official license provenance");
  }
  const licensePath = path.join(kitRoot, "skills", licenseReference);
  if (
    !fs.existsSync(licensePath)
    || crypto.createHash("sha256").update(fs.readFileSync(licensePath)).digest("hex") !== official.license_sha256
  ) {
    throw new Error("Android official license provenance changed");
  }
  for (const name of snapshotNames) {
    const provenance = snapshots[name];
    const upstreamName = provenance?.upstream_name ?? name;
    if (
      !isSafeSkillName(name)
      || !isSafeSkillName(upstreamName)
      || !isSafeRelativePath(provenance?.upstream_path)
      || !/^[0-9a-f]{64}$/.test(provenance?.upstream_tree_hash || "")
      || !/^[0-9a-f]{64}$/.test(provenance?.upstream_skill_sha256 || "")
      || !["bundled-adapter", "install-time-indexed"].includes(provenance?.snapshot_mode)
    ) {
      throw new Error(`invalid Android official skill provenance: ${name}`);
    }
    if (provenance.snapshot_mode === "install-time-indexed") {
      if (provenance.project_tree_hash !== null) {
        throw new Error(`invalid Android indexed skill project hash: ${name}`);
      }
      continue;
    }
    if (!/^[0-9a-f]{64}$/.test(provenance.project_tree_hash || "")) {
      throw new Error(`invalid Android bundled skill project hash: ${name}`);
    }
    const skillRoot = path.join(kitRoot, "skills", name);
    if (!fs.existsSync(path.join(skillRoot, "SKILL.md"))) {
      throw new Error(`missing Android bundled skill snapshot: ${name}`);
    }
    assertSkillSourceProvenance(kitRoot, skillRoot);
    if (hashSkillTree(skillRoot) !== provenance.project_tree_hash) {
      throw new Error(`Android bundled skill snapshot changed: ${name}`);
    }
  }
}

function isSafeRelativePath(value) {
  return typeof value === "string"
    && value.length > 0
    && !path.isAbsolute(value)
    && !value.includes("\\")
    && value.split("/").every((part) => part && part !== "." && part !== "..");
}

export function hashSkillTree(root) {
  if (fs.lstatSync(root).isSymbolicLink()) {
    throw new Error(`skill source may not be a symlink: ${root}`);
  }
  const hash = crypto.createHash("sha256");
  for (const file of skillTreeFiles(root)) {
    const relative = path.relative(root, file).split(path.sep).join("/");
    hash.update(relative);
    hash.update("\0");
    hash.update(fs.readFileSync(file));
    hash.update("\0");
  }
  return hash.digest("hex");
}

function inspectExternalSkillCandidate(candidate, name, rejected) {
  if (!candidate || !fs.existsSync(path.join(candidate.root, "SKILL.md"))) return null;
  try {
    assertSkillSourceProvenance(candidate.authority_root, candidate.root);
    assertSelectedSkillLogicalName(candidate.root, name);
    return { ...candidate, tree_hash: hashSkillTree(candidate.root) };
  } catch (error) {
    const source = candidate.host ? `${candidate.kind}:${candidate.host}` : candidate.kind;
    rejected.push(`${source} ${candidate.root}: ${error instanceof Error ? error.message : String(error)}`);
    return null;
  }
}

function authenticatedPreviousExternalSource({ candidates, hostRoots, name, previous, rejected }) {
  if (typeof previous.tree_hash !== "string" || !/^[0-9a-f]{64}$/.test(previous.tree_hash)) {
    throw new Error(`invalid pinned external skill provenance: ${name}`);
  }
  if (previous.source === "shared") {
    return inspectExternalSkillCandidate(candidates.get("shared"), name, rejected);
  }
  if (previous.source !== "host-bootstrap" || typeof previous.source_host !== "string") {
    throw new Error(`invalid pinned external skill provenance: ${name}`);
  }
  const sourceRoot = hostRoots.find((candidate) => candidate.host === previous.source_host);
  if (!sourceRoot) {
    throw new Error(`invalid pinned external skill provenance: ${name}`);
  }
  return inspectExternalSkillCandidate({
    kind: "host-bootstrap",
    root: externalSkillRoot(sourceRoot.root, name),
    host: previous.source_host,
    authority_root: sourceRoot.authorityRoot,
  }, name, rejected);
}

function validatedExternalSkillCatalog(source) {
  let metadata;
  try {
    metadata = fs.lstatSync(source.root);
  } catch (error) {
    if (error?.code === "ENOENT") return [];
    throw error;
  }
  if (metadata.isSymbolicLink() || !metadata.isDirectory()) return [];
  const names = [];
  for (const entry of fs.readdirSync(source.root, { withFileTypes: true }).sort((left, right) => (
    compareCodePoints(left.name, right.name)
  ))) {
    if (!entry.isDirectory() || !isSafeSkillName(entry.name)) continue;
    const candidate = inspectExternalSkillCandidate({
      kind: source.kind,
      root: path.join(source.root, entry.name),
      host: source.host,
      authority_root: source.authorityRoot,
    }, entry.name, []);
    if (candidate) names.push(portableSkillCasefold(entry.name));
  }
  return names;
}

function assertSelectedSkillLogicalName(root, expectedName) {
  const skillPath = path.join(root, "SKILL.md");
  const metadata = parseSkillFrontmatter(fs.readFileSync(skillPath, "utf8"));
  const declaredName = String(metadata.name || path.basename(root));
  if (
    !isSafeSkillName(declaredName)
    || portableSkillCasefold(declaredName) !== portableSkillCasefold(expectedName)
  ) {
    throw new Error(
      `skill source logical name mismatch: expected ${portableSkillCasefold(expectedName)}, got ${declaredName}`,
    );
  }
}

function assertSkillSourceProvenance(authorityRoot, skillRoot) {
  const lexicalAuthority = path.resolve(authorityRoot);
  const lexicalSkillRoot = path.resolve(skillRoot);
  const relative = path.relative(lexicalAuthority, lexicalSkillRoot);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(`skill source escapes authority root: ${skillRoot}`);
  }
  let cursor = lexicalAuthority;
  for (const part of relative.split(path.sep).filter(Boolean)) {
    cursor = path.join(cursor, part);
    const stat = fs.lstatSync(cursor);
    if (stat.isSymbolicLink()) {
      throw new Error(`skill source may not use symlink ancestors: ${cursor}`);
    }
    if (!stat.isDirectory()) {
      throw new Error(`skill source ancestor is not a directory: ${cursor}`);
    }
  }
  const skillPath = path.join(lexicalSkillRoot, "SKILL.md");
  const skillStat = fs.lstatSync(skillPath);
  if (skillStat.isSymbolicLink() || !skillStat.isFile()) {
    throw new Error(`skill source SKILL.md must be a regular file: ${skillPath}`);
  }
  const realAuthority = fs.realpathSync(lexicalAuthority);
  const realSkillRoot = fs.realpathSync(lexicalSkillRoot);
  const realRelative = path.relative(realAuthority, realSkillRoot);
  if (realRelative.startsWith("..") || path.isAbsolute(realRelative)) {
    throw new Error(`skill source escapes authority root: ${skillRoot}`);
  }
}

function skillTreeFiles(root) {
  const files = [];
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const candidate = path.join(root, entry.name);
    if (entry.isSymbolicLink()) {
      throw new Error(`skill source may not contain symlinks: ${candidate}`);
    }
    if (entry.isDirectory()) {
      files.push(...skillTreeFiles(candidate));
    } else if (entry.isFile()) {
      files.push(candidate);
    } else {
      throw new Error(`skill source must contain only regular files and directories: ${candidate}`);
    }
  }
  return files.sort(compareCodePoints);
}

function compareCodePoints(left, right) {
  const a = Array.from(String(left), (char) => char.codePointAt(0));
  const b = Array.from(String(right), (char) => char.codePointAt(0));
  for (let index = 0; index < Math.min(a.length, b.length); index += 1) {
    if (a[index] !== b[index]) return a[index] - b[index];
  }
  return a.length - b.length;
}

export function resolveRuntimeSkillPlan(index, { phaseId, changedFiles = [], taskScope = "" } = {}) {
  const codePhases = new Set([
    "implement", "implement-fix", "red", "green", "refactor", "fix-loop",
    "final-review", "review", "pr-comment-fix", "pr-ci-fix", "multi-review",
    "architecture-review",
  ]);
  const normalizedTaskScope = normalizeTaskScope(taskScope);
  if (!codePhases.has(phaseId)) {
    return { phase: phaseId, active_profiles: [], touched_profiles: [], changed_files: [], task_scope: normalizedTaskScope, skills: [], missing: [], missing_profiles: [] };
  }
  if (!index?.selection || typeof index.selection !== "object" || Array.isArray(index.selection)) {
    return { phase: phaseId, active_profiles: [], touched_profiles: [], changed_files: [], task_scope: normalizedTaskScope, skills: [], missing: [], missing_profiles: [] };
  }
  const selection = index?.selection || {};
  const activeProfiles = uniqueLogicalNames(selection.profiles || []).sort(compareCodePoints);
  const installedProfiles = new Set(
    selection.skill_profiles === undefined || selection.skill_profiles === null
      ? activeProfiles
      : Array.isArray(selection.skill_profiles)
        ? uniqueLogicalNames(selection.skill_profiles).sort(compareCodePoints)
        : [],
  );
  const normalizedFiles = uniqueStrings(changedFiles.map((file) => String(file).replaceAll("\\", "/"))).sort(compareCodePoints);
  const routing = selection.profile_routing || {};
  const touchedProfiles = resolveTouchedProfiles(activeProfiles, normalizedFiles, normalizedTaskScope, routing);
  const missingProfiles = [...touchedProfiles]
    .filter((profile) => !installedProfiles.has(profile))
    .sort(compareCodePoints);
  const required = new LogicalNameSet(["code-generation-discipline"]);
  for (const profile of touchedProfiles) {
    for (const name of logicalNameValues(selection.required_review?.[profile])) {
      required.add(name);
    }
    const mode = reviewSkillPhase(phaseId) ? "review" : "implementation";
    const catalog = new LogicalNameSet(selection.conditional_skills?.[profile]?.[mode] || []);
    for (const route of routing.profiles?.[profile]?.skill_routes || []) {
      if (!filesMatchRules(normalizedFiles, route.file_rules || [])
        && !taskMatchesTerms(normalizedTaskScope, route.task_terms || [])) continue;
      for (const name of logicalNameValues(route.skills)) {
        if (catalog.has(name)) required.add(name);
      }
    }
  }
  const byName = indexSkillsByLogicalName(index?.skills || [], "installed skill index");
  const explicit = new LogicalNameSet(selection.explicit_skills || []);
  for (const [name, skill] of byName) {
    const phases = logicalNameValues(skill.workflowPhases).map(String);
    const phaseMatches = phases.length === 0 || phases.includes(phaseId);
    if (explicit.has(name)) {
      required.add(name);
    } else if (skill.activation === "always" && phaseMatches) {
      required.add(name);
    } else if (
      skill.activation === "conditional"
      && phaseMatches
      && (
        taskMatchesTerms(normalizedTaskScope, logicalNameValues(skill.taskTerms).map(String))
        || normalizedFiles.some((file) => logicalNameValues(skill.pathGlobs).some((glob) => pathMatchesGlob(file, glob)))
      )
    ) {
      required.add(name);
    }
  }
  let dependencyAdded = true;
  while (dependencyAdded) {
    dependencyAdded = false;
    for (const name of [...required]) {
      const skill = byName.get(name);
      for (const dependency of logicalNameValues(skill?.requires)) {
        const logicalDependency = portableSkillCasefold(dependency);
        if (!required.has(logicalDependency)) {
          required.add(logicalDependency);
          dependencyAdded = true;
        }
      }
    }
  }
  for (const name of EXPLICIT_ONLY_SKILLS) {
    if (!explicit.has(name)) required.delete(name);
  }
  const skills = [];
  const missing = [];
  for (const name of [...required].sort(compareCodePoints)) {
    const logicalName = portableSkillCasefold(name);
    const skill = byName.get(logicalName);
    if (!skill) {
      missing.push(logicalName);
      continue;
    }
    skills.push({ name: logicalName, path: skill.path, tree_hash: skill.tree_hash });
  }
  return {
    phase: phaseId,
    active_profiles: activeProfiles,
    touched_profiles: [...touchedProfiles].sort(compareCodePoints),
    changed_files: normalizedFiles,
    task_scope: normalizedTaskScope,
    skills,
    missing,
    missing_profiles: missingProfiles,
  };
}

function pathMatchesGlob(file, glob) {
  const value = String(glob || "");
  if (!value || value.includes("\\") || path.isAbsolute(value) || value.split("/").includes("..")) {
    return false;
  }
  const escaped = value.replace(/[.+^${}()|[\]\\]/g, "\\$&")
    .replaceAll("**", "\0")
    .replaceAll("*", "[^/]*")
    .replaceAll("?", "[^/]")
    .replaceAll("\0", ".*");
  return new RegExp(`^${escaped}$`).test(file);
}

function reviewSkillPhase(phaseId) {
  return new Set(["final-review", "review", "multi-review", "architecture-review"]).has(phaseId);
}

function resolveTouchedProfiles(activeProfiles, changedFiles, taskScope, routing) {
  if (changedFiles.length === 0 && !taskScope) return new Set(activeProfiles);
  const touched = new Set();
  const taskProfiles = resolveProfilesForTask(activeProfiles, taskScope, routing);
  for (const profile of resolveProfilesForFiles(activeProfiles, changedFiles, routing)) {
    if (taskOverridesGenericFileProfile(profile, taskProfiles, changedFiles, routing)) continue;
    touched.add(profile);
  }
  for (const profile of taskProfiles) {
    touched.add(profile);
  }
  for (const profile of activeProfiles) {
    for (const escalation of routing.escalations?.[profile] || []) {
      if (!filesMatchRules(changedFiles, escalation.file_rules || [])
        && !taskMatchesTerms(taskScope, escalation.task_terms || [])) continue;
      touched.add(profile);
      if (escalation.profile) touched.add(escalation.profile);
    }
  }
  if (touched.size === 0 && changedFiles.length === 0) {
    for (const profile of activeProfiles) touched.add(profile);
  } else if (touched.size === 0 && activeProfiles.length === 1) {
    touched.add(activeProfiles[0]);
  }
  return touched;
}

function taskOverridesGenericFileProfile(profile, taskProfiles, changedFiles, routing) {
  if (taskProfiles.includes(profile)) return false;
  const config = routing.profiles?.[profile] || {};
  const family = String(config.family || profile);
  const hasExplicitFamilyProfile = taskProfiles.some((candidate) => {
    const candidateConfig = routing.profiles?.[candidate] || {};
    return String(candidateConfig.family || candidate) === family;
  });
  if (!hasExplicitFamilyProfile) return false;
  const score = changedFiles.reduce(
    (best, file) => Math.max(best, bestFileRuleScore(file, config.file_rules || [])),
    -1,
  );
  return score <= GENERIC_FILE_RULE_SCORE;
}

function resolveProfilesForFiles(activeProfiles, changedFiles, routing) {
  const touched = new Set();
  for (const file of changedFiles) {
    const candidates = activeProfiles
      .map((profile) => {
        const config = routing.profiles?.[profile] || {};
        return {
          profile,
          family: String(config.family || profile),
          priority: Number.isFinite(config.priority) ? config.priority : 0,
          fallback: config.fallback === true,
          score: bestFileRuleScore(file, config.file_rules || []),
        };
      })
      .filter((candidate) => candidate.score >= 0);
    const scopedCandidates = candidates.some((candidate) => !candidate.fallback)
      ? candidates.filter((candidate) => !candidate.fallback)
      : candidates;
    for (const candidate of highestPriorityPerFamily(scopedCandidates)) {
      touched.add(candidate.profile);
    }
  }
  return touched;
}

function resolveProfilesForTask(activeProfiles, taskScope, routing) {
  if (!taskScope) return [];
  const candidates = activeProfiles
    .map((profile) => {
      const config = routing.profiles?.[profile] || {};
      const matches = taskTermMatches(taskScope, config.task_terms || []);
      return {
        profile,
        family: String(config.family || profile),
        priority: Number.isFinite(config.priority) ? config.priority : 0,
        score: matches.reduce((best, match) => Math.max(best, match.length), -1),
        matches,
      };
    })
    .filter((candidate) => candidate.score >= 0);
  return explicitTaskProfiles(candidates);
}

function explicitTaskProfiles(candidates) {
  const matches = candidates
    .flatMap((candidate) => candidate.matches.map((match) => ({ ...candidate, ...match })))
    .sort((left, right) =>
      right.length - left.length
      || left.start - right.start
      || right.priority - left.priority
      || (left.profile < right.profile ? -1 : left.profile > right.profile ? 1 : 0),
    );
  const claimed = [];
  const selected = new Set();
  for (const match of matches) {
    const overlapsClaimedTerm = claimed.some((current) =>
      current.family === match.family
      && match.start < current.end
      && current.start < match.end,
    );
    if (overlapsClaimedTerm) continue;
    claimed.push(match);
    selected.add(match.profile);
  }
  return candidates
    .filter((candidate) => selected.has(candidate.profile))
    .map((candidate) => candidate.profile);
}

function highestPriorityPerFamily(candidates) {
  const winners = new Map();
  for (const candidate of candidates) {
    const current = winners.get(candidate.family);
    if (!current
      || candidate.score > current.score
      || (candidate.score === current.score && candidate.priority > current.priority)
      || (candidate.score === current.score && candidate.priority === current.priority && candidate.profile < current.profile)) {
      winners.set(candidate.family, candidate);
    }
  }
  return [...winners.values()];
}

function filesMatchRules(files, rules) {
  return rules.length > 0 && files.some((file) => rules.some((rule) => fileMatchesRule(file, rule)));
}

function bestFileRuleScore(file, rules) {
  let best = -1;
  for (const rule of rules) {
    if (!fileMatchesRule(file, rule)) continue;
    let score = rule.any_file === true ? 1 : 0;
    if (Array.isArray(rule.extensions) && rule.extensions.length > 0) score += 20;
    if (Array.isArray(rule.names) && rule.names.length > 0) score += 50;
    if (Array.isArray(rule.path_terms) && rule.path_terms.length > 0) score += 60;
    if (Array.isArray(rule.prefixes) && rule.prefixes.length > 0) score += 80;
    best = Math.max(best, score);
  }
  return best;
}

function normalizeTaskScope(taskScope) {
  return String(taskScope || "").normalize("NFKC").trim().replace(/\s+/g, " ");
}

function taskMatchesTerms(taskScope, terms) {
  return bestTaskTermScore(taskScope, terms) >= 0;
}

function bestTaskTermScore(taskScope, terms) {
  return taskTermMatches(taskScope, terms)
    .reduce((best, match) => Math.max(best, match.length), -1);
}

function taskTermMatches(taskScope, terms) {
  if (!taskScope || !Array.isArray(terms)) return [];
  const normalized = taskScope.toLowerCase();
  return terms.flatMap((term) => {
    const candidate = String(term).normalize("NFKC").trim().toLowerCase();
    return candidate ? taskTermRanges(normalized, candidate) : [];
  });
}

function taskTermRanges(taskScope, term) {
  const matches = [];
  let offset = taskScope.indexOf(term);
  while (offset >= 0) {
    const before = offset === 0 ? "" : taskScope[offset - 1];
    const afterOffset = offset + term.length;
    const after = afterOffset >= taskScope.length ? "" : taskScope[afterOffset];
    const startsWithWord = /[a-z0-9]/.test(term[0]);
    const endsWithWord = /[a-z0-9]/.test(term[term.length - 1]);
    if ((!startsWithWord || !/[a-z0-9]/.test(before))
      && (!endsWithWord || !/[a-z0-9]/.test(after))) {
      matches.push({ start: offset, end: afterOffset, length: term.length });
    }
    offset = taskScope.indexOf(term, offset + 1);
  }
  return matches;
}

function fileMatchesRule(file, rule) {
  const normalized = file.toLowerCase();
  const basename = path.posix.basename(normalized);
  const checks = [];
  if (rule.any_file === true) checks.push(true);
  if (Array.isArray(rule.prefixes)) {
    checks.push(rule.prefixes.some((value) => normalized.startsWith(String(value).toLowerCase())));
  }
  if (Array.isArray(rule.extensions)) {
    checks.push(rule.extensions.some((value) => normalized.endsWith(String(value).toLowerCase())));
  }
  if (Array.isArray(rule.names)) {
    checks.push(rule.names.some((value) => basename === String(value).toLowerCase()));
  }
  if (Array.isArray(rule.path_terms)) {
    checks.push(rule.path_terms.some((value) => normalized.includes(String(value).toLowerCase())));
  }
  return checks.length > 0 && checks.every(Boolean);
}

function validateProfiles(profiles, kitRoot) {
  validateProfileNames(profiles);
  for (const profile of profiles) {
    if (!PROFILE_SKILLS.has(profile) && !fs.existsSync(path.join(kitRoot, "profiles", `${profile}.yaml`))) {
      throw new Error(`unknown profile: ${profile}`);
    }
  }
}

function validateProfileNames(values) {
  for (const value of values) {
    if (typeof value !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(value)) {
      throw new Error(`unsafe profile name: ${value}`);
    }
  }
}

function validateSafeNames(values, label) {
  for (const value of values) {
    if (!isSafeSkillName(value)) {
      throw new Error(`unsafe ${label} name: ${value}`);
    }
  }
}

function addProjectSkillNames(names, projectRoot, kitRoot) {
  const roots = [path.join(projectRoot, ".agent-flow", "local-skills")];
  if (!sameDirectory(projectRoot, kitRoot)) {
    roots.push(path.join(projectRoot, "skills"));
  }
  for (const base of roots) {
    if (!fs.existsSync(base)) continue;
    for (const skill of projectSkillCatalog(base, projectRoot)) {
      names.add(skill.logicalName);
    }
  }
}

function sameDirectory(left, right) {
  try {
    return fs.realpathSync(left) === fs.realpathSync(right);
  } catch {
    return path.resolve(left) === path.resolve(right);
  }
}

function projectSkillRoot(base, name, authorityRoot) {
  const logicalName = portableSkillCasefold(name);
  if (!fs.existsSync(base)) return path.join(base, logicalName);
  const matches = projectSkillCatalog(base, authorityRoot)
    .filter((skill) => skill.logicalName === logicalName);
  if (matches.length === 1) return matches[0].root;
  return path.join(base, logicalName);
}

function externalSkillRoot(base, name) {
  const logicalName = portableSkillCasefold(name);
  const direct = path.join(base, logicalName);
  if (!fs.existsSync(base)) return direct;
  const matches = fs.readdirSync(base, { withFileTypes: true })
    .filter((entry) => (
      entry.isDirectory()
      && isSafeSkillName(entry.name)
      && portableSkillCasefold(entry.name) === logicalName
    ))
    .map((entry) => path.join(base, entry.name))
    .filter((root) => fs.existsSync(path.join(root, "SKILL.md")))
    .sort(compareCodePoints);
  if (matches.length > 1) {
    throw new Error(`conflicting external skill paths: ${logicalName} (${matches.join(", ")})`);
  }
  if (matches.length === 1) return matches[0];
  if (fs.existsSync(path.join(direct, "SKILL.md"))) {
    const actualName = path.basename(fs.realpathSync(direct));
    if (!isSafeSkillName(actualName)) {
      throw new Error(`unsafe external skill path: ${actualName}`);
    }
  }
  return direct;
}

function projectSkillCatalog(base, authorityRoot) {
  assertSkillSourceDirectoryProvenance(authorityRoot, base);
  const byLogicalName = new Map();
  const entries = fs.readdirSync(base, { withFileTypes: true })
    .sort((left, right) => compareCodePoints(left.name, right.name));
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const root = path.join(base, entry.name);
    const skillPath = path.join(root, "SKILL.md");
    if (!fs.existsSync(skillPath)) continue;
    const metadata = parseSkillFrontmatter(fs.readFileSync(skillPath, "utf8"));
    const declaredName = String(metadata.name || entry.name);
    if (!isSafeSkillName(declaredName)) {
      throw new Error(`unsafe project-local skill name: ${declaredName}`);
    }
    const logicalName = portableSkillCasefold(declaredName);
    const candidate = { logicalName, root };
    const existing = byLogicalName.get(logicalName);
    if (existing) {
      const paths = [existing.root, root].sort(compareCodePoints);
      throw new Error(
        `conflicting project-local skill paths: ${logicalName} (${paths.join(", ")})`,
      );
    }
    byLogicalName.set(logicalName, candidate);
  }
  return [...byLogicalName.values()].sort((left, right) => compareCodePoints(left.logicalName, right.logicalName));
}

function assertSkillSourceDirectoryProvenance(authorityRoot, directory) {
  const lexicalAuthority = path.resolve(authorityRoot);
  const lexicalDirectory = path.resolve(directory);
  const relative = path.relative(lexicalAuthority, lexicalDirectory);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(`skill source escapes authority root: ${directory}`);
  }
  let cursor = lexicalAuthority;
  for (const part of relative.split(path.sep).filter(Boolean)) {
    cursor = path.join(cursor, part);
    const stat = fs.lstatSync(cursor);
    if (stat.isSymbolicLink()) {
      throw new Error(`skill source may not use symlink ancestors: ${cursor}`);
    }
    if (!stat.isDirectory()) {
      throw new Error(`skill source ancestor is not a directory: ${cursor}`);
    }
  }
  const realAuthority = fs.realpathSync(lexicalAuthority);
  const realDirectory = fs.realpathSync(lexicalDirectory);
  const realRelative = path.relative(realAuthority, realDirectory);
  if (realRelative.startsWith("..") || path.isAbsolute(realRelative)) {
    throw new Error(`skill source escapes authority root: ${directory}`);
  }
}

function installationProfiles(profiles, projectRoot, routing) {
  const resolved = new Set(profiles || []);
  for (const profile of profiles || []) {
    for (const escalation of routing.escalations?.[profile] || []) {
      const roots = uniqueStrings(escalation.install_roots || []);
      if (roots.length > 0 && roots.some((root) => fs.existsSync(path.join(projectRoot, root)))) {
        resolved.add(escalation.profile);
      }
    }
  }
  return [...resolved].sort();
}

function profileRequirementProfiles(profiles, skillProfiles, routing) {
  const resolved = new Set(skillProfiles || []);
  for (const profile of profiles || []) {
    for (const escalation of routing.escalations?.[profile] || []) {
      if (escalation.profile) resolved.add(escalation.profile);
    }
  }
  return [...resolved].sort();
}

export function loadProfileRouting(kitRoot) {
  const routingPath = path.join(kitRoot, "skills", "profile-routing.json");
  if (!fs.existsSync(routingPath)) {
    throw new Error(`missing profile routing config: ${routingPath}`);
  }
  let routing;
  try {
    routing = JSON.parse(fs.readFileSync(routingPath, "utf8"));
  } catch (error) {
    throw new Error(`invalid profile routing config: ${error.message}`);
  }
  if (routing?.version !== 1 || !routing.profiles || !routing.escalations) {
    throw new Error("invalid profile routing config shape");
  }
  for (const [profile, config] of Object.entries(routing.profiles)) {
    validateRoutingName(profile, "profile");
    validateRoutingName(config.family || profile, "family");
    if (config.priority !== undefined && !Number.isInteger(config.priority)) {
      throw new Error(`invalid profile priority: ${profile}`);
    }
    if (config.fallback !== undefined && typeof config.fallback !== "boolean") {
      throw new Error(`invalid profile fallback: ${profile}`);
    }
    validateTaskTerms(config.task_terms, `profile ${profile}`);
    validateFileRules(config.file_rules, `profile ${profile}`);
    const conditional = conditionalSkillsFromProfileYaml(path.join(kitRoot, "profiles", `${profile}.yaml`));
    const routed = new LogicalNameSet();
    const conditionalNames = new LogicalNameSet([...conditional.implementation, ...conditional.review]);
    for (const route of config.skill_routes || []) {
      validateRoutingName(route.id, "route");
      validateTaskTerms(route.task_terms, `route ${profile}/${route.id}`);
      validateFileRules(route.file_rules, `route ${profile}/${route.id}`);
      if (!Array.isArray(route.skills) || route.skills.length === 0) {
        throw new Error(`invalid empty skill route: ${profile}/${route.id}`);
      }
      for (const skill of route.skills) {
        validateRoutingName(skill, "skill");
        const logicalSkill = portableSkillCasefold(skill);
        if (conditionalNames.has(logicalSkill)) routed.add(logicalSkill);
      }
    }
    for (const skill of conditionalNames) {
      if (!routed.has(skill)) throw new Error(`conditional skill has no deterministic route: ${profile}/${skill}`);
    }
  }
  for (const [profile, escalations] of Object.entries(routing.escalations)) {
    validateRoutingName(profile, "profile");
    if (!Array.isArray(escalations)) throw new Error(`invalid escalations: ${profile}`);
    for (const escalation of escalations) {
      validateRoutingName(escalation.profile, "profile");
      validateTaskTerms(escalation.task_terms, `escalation ${profile}/${escalation.profile}`);
      validateFileRules(escalation.file_rules, `escalation ${profile}/${escalation.profile}`);
      for (const root of escalation.install_roots || []) {
        if (!isSafeRelativeRoot(root)) throw new Error(`unsafe install root: ${root}`);
      }
    }
  }
  return routing;
}

function validateRoutingName(value, label) {
  if (!isSafeSkillName(value)) throw new Error(`unsafe routing ${label}: ${value}`);
}

function validateTaskTerms(terms, label) {
  if (terms === undefined) return;
  if (!Array.isArray(terms) || !terms.every((term) => typeof term === "string" && term.trim().length > 0)) {
    throw new Error(`invalid task terms: ${label}`);
  }
}

function validateFileRules(rules, label) {
  if (!Array.isArray(rules) || rules.length === 0) throw new Error(`invalid file rules: ${label}`);
  for (const rule of rules) {
    const keys = ["prefixes", "extensions", "names", "path_terms"];
    const populated = keys.filter((key) => Array.isArray(rule?.[key]) && rule[key].length > 0);
    if (rule?.any_file !== true && populated.length === 0) throw new Error(`empty file rule: ${label}`);
    for (const key of populated) {
      if (!rule[key].every((value) => typeof value === "string" && value.length > 0)) {
        throw new Error(`invalid ${key} rule: ${label}`);
      }
    }
  }
}

function isSafeRelativeRoot(value) {
  return typeof value === "string"
    && value.length > 0
    && value !== "."
    && value !== ".."
    && !path.isAbsolute(value)
    && !value.split(/[\\/]+/).includes("..");
}

function requiredReviewSkills(profiles, kitRoot) {
  const result = {};
  for (const profile of profiles) {
    result[profile] = requiredReviewSkillsFromProfileYaml(path.join(kitRoot, "profiles", `${profile}.yaml`));
  }
  return result;
}

function conditionalProfileSkills(profiles, kitRoot) {
  const result = {};
  for (const profile of profiles) {
    result[profile] = conditionalSkillsFromProfileYaml(path.join(kitRoot, "profiles", `${profile}.yaml`));
  }
  return result;
}

function conditionalSkillsFromProfileYaml(profilePath) {
  if (!fs.existsSync(profilePath)) return { implementation: [], review: [] };
  const text = fs.readFileSync(profilePath, "utf8");
  const referencedSources = new Set();
  let inSkills = false;
  let inRequiredReview = false;
  for (const line of text.split(/\r?\n/)) {
    if (/^\S/.test(line)) {
      inSkills = line.trim() === "skills:";
      inRequiredReview = false;
      continue;
    }
    if (!inSkills) continue;
    if (/^  required_review:\s*$/.test(line)) {
      inRequiredReview = true;
      continue;
    }
    if (/^  \S/.test(line)) inRequiredReview = false;
    if (!inRequiredReview) continue;
    const match = line.match(/^\s+skills_from:\s*([A-Za-z0-9_-]+)\.(implementation|review)\s*$/);
    if (match) referencedSources.add(match[1]);
  }
  const catalogs = skillCatalogsFromProfileYaml(text);
  const implementation = new LogicalNameSet();
  const review = new LogicalNameSet();
  for (const source of referencedSources) {
    for (const name of catalogs[source]?.implementation || []) implementation.add(name);
    for (const name of catalogs[source]?.review || []) review.add(name);
  }
  return {
    implementation: [...implementation].sort(),
    review: [...review].sort(),
  };
}

function skillCatalogsFromProfileYaml(text) {
  const result = {};
  let source = null;
  let mode = null;
  for (const line of text.split(/\r?\n/)) {
    const top = line.match(/^([A-Za-z0-9_-]+):\s*$/);
    if (top) {
      source = top[1];
      mode = null;
      continue;
    }
    const section = line.match(/^  (implementation|review):\s*$/);
    if (section && source) {
      mode = section[1];
      result[source] ||= { implementation: [], review: [] };
      continue;
    }
    if (/^  \S/.test(line)) mode = null;
    const item = line.match(/^    - skill:\s*(.*?)\s*$/);
    if (item && source && mode) {
      const name = stripQuotes(item[1].trim());
      if (!isSafeSkillName(name)) {
        throw new Error(`unsafe profile skill name: ${name}`);
      }
      result[source][mode].push(portableSkillCasefold(name));
    }
  }
  return result;
}

function requiredReviewSkillsFromProfileYaml(profilePath) {
  if (!fs.existsSync(profilePath)) return [];
  const text = fs.readFileSync(profilePath, "utf8");
  const values = [];
  let inSkills = false;
  let inRequiredReview = false;
  for (const line of text.split(/\r?\n/)) {
    if (/^\S/.test(line)) {
      inSkills = line.trim() === "skills:";
      inRequiredReview = false;
      continue;
    }
    if (!inSkills) continue;
    if (/^  required_review:\s*$/.test(line)) {
      inRequiredReview = true;
      continue;
    }
    if (/^  \S/.test(line)) {
      inRequiredReview = false;
    }
    if (!inRequiredReview) continue;
    const match = line.match(/^\s+skills:\s*\[([^\]]*)\]\s*$/);
    if (match) {
      const names = match[1]
        .split(",")
        .map((value) => stripQuotes(value.trim()))
        .filter(Boolean);
      validateSafeNames(names, "profile skill");
      values.push(...names);
    }
  }
  return uniqueLogicalNames(values).sort(compareCodePoints);
}

function yamlListSection(text, section) {
  const values = [];
  let active = false;
  for (const line of text.split(/\r?\n/)) {
    if (/^\S/.test(line)) {
      active = line.trim() === `${section}:`;
      continue;
    }
    if (!active) continue;
    const match = line.match(/^\s+-\s*([A-Za-z0-9._-]+)\s*$/);
    if (match) values.push(match[1]);
  }
  return values;
}

function yamlMapSection(text, section) {
  const values = {};
  let active = false;
  for (const line of text.split(/\r?\n/)) {
    if (/^\S/.test(line)) {
      active = line.trim() === `${section}:`;
      continue;
    }
    if (!active) continue;
    const match = line.match(/^\s+([A-Za-z0-9._-]+):\s*(.*?)\s*$/);
    if (match) values[match[1]] = yamlScalarValue(match[2]);
  }
  return values;
}

function yamlScalarValue(value) {
  const raw = value.trim();
  if (
    raw.length >= 2
    && ((raw.startsWith('"') && raw.endsWith('"')) || (raw.startsWith("'") && raw.endsWith("'")))
  ) {
    return raw.slice(1, -1);
  }
  return raw.replace(/\s+#.*$/, "").trim();
}

function skillDependencies(name, { kitRoot, projectRoot }) {
  const dependencies = [
    ...(SKILL_DEPENDENCIES.get(portableSkillCasefold(name)) || []),
    ...metadataDependenciesForSkill(name, { kitRoot, projectRoot }),
  ];
  validateSafeNames(dependencies, "skill dependency");
  return dependencies.map(portableSkillCasefold);
}

function metadataDependenciesForSkill(name, { kitRoot, projectRoot }) {
  const skillPath = authoritativeProjectMetadataPath(name, { kitRoot, projectRoot });
  return skillPath ? dependenciesFromSkillPath(skillPath) : [];
}

function authoritativeProjectMetadataPath(name, { kitRoot, projectRoot }) {
  if (!projectRoot) return null;
  const roots = [path.join(projectRoot, ".agent-flow", "local-skills")];
  if (!sameDirectory(projectRoot, kitRoot)) roots.push(path.join(projectRoot, "skills"));
  for (const base of roots) {
    const root = projectSkillRoot(base, name, projectRoot);
    const skillPath = path.join(root, "SKILL.md");
    if (fs.existsSync(skillPath)) return skillPath;
  }
  return null;
}

function selectedSkillDependencies(name, selectedRoot) {
  const dependencies = [
    ...(SKILL_DEPENDENCIES.get(portableSkillCasefold(name)) || []),
    ...dependenciesFromSkillPath(path.join(selectedRoot, "SKILL.md")),
  ];
  validateSafeNames(dependencies, "skill dependency");
  return uniqueLogicalNames(dependencies);
}

function resolvedSkillDependencyClosure(roots, entries) {
  const byName = indexSkillsByLogicalName(entries, "resolved skill dependency closure");
  const pending = [...roots];
  const closure = new LogicalNameSet();
  while (pending.length > 0) {
    const name = portableSkillCasefold(pending.shift());
    if (closure.has(name)) continue;
    const entry = byName.get(name);
    if (!entry) continue;
    closure.add(name);
    for (const dependency of selectedSkillDependencies(name, entry.source_path)) {
      if (!closure.has(dependency)) pending.push(dependency);
    }
  }
  return closure;
}

function dependenciesFromSkillPath(skillPath) {
  if (!fs.existsSync(skillPath)) return [];
  const metadata = parseSkillFrontmatter(fs.readFileSync(skillPath, "utf8"));
  return [
    ...arrayValue(metadata.dependencies),
    ...arrayValue(metadata.requires),
  ];
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
      const match = line.match(/^\s+-\s*(.*?)\s*$/);
      if (match) {
        const name = stripQuotes(match[1].trim());
        if (!isSafeSkillName(name)) {
          throw new Error(`unsafe profile skill name: ${name}`);
        }
        values.push(portableSkillCasefold(name));
      }
    }
  }
  return uniqueLogicalNames(values);
}

function splitSkillFrontmatter(text) {
  const match = String(text).match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/);
  return match?.[1] ?? null;
}

export function parseSkillFrontmatter(text) {
  const frontmatter = splitSkillFrontmatter(text);
  return frontmatter === null ? {} : parseSimpleYaml(frontmatter);
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
  if (typeof value !== "string") return false;
  const name = String(value);
  return /^[A-Za-z0-9._-]+$/.test(name)
    && !name.startsWith(".")
    && !name.includes("..");
}

export function isPortableSkillName(value) {
  return isSafeSkillName(value);
}

export function portableSkillCasefold(value) {
  if (!isSafeSkillName(value)) {
    throw new Error(`unsafe skill name: ${value}`);
  }
  return String(value).toLowerCase();
}

class LogicalNameSet extends Set {
  constructor(values = []) {
    super();
    for (const value of logicalNameValues(values)) this.add(value);
  }

  add(value) {
    return super.add(portableSkillCasefold(value));
  }

  has(value) {
    return super.has(portableSkillCasefold(value));
  }

  delete(value) {
    return super.delete(portableSkillCasefold(value));
  }
}

function uniqueLogicalNames(values) {
  return [...new LogicalNameSet(values)];
}

function logicalNameValues(values) {
  if (Array.isArray(values) || values instanceof Set) return values;
  return [];
}

function indexSkillsByLogicalName(skills, label) {
  const byName = new Map();
  if (!Array.isArray(skills)) throw new Error(`invalid ${label} skills`);
  const entries = skills;
  for (const skill of entries) {
    if (!skill || typeof skill !== "object" || !isSafeSkillName(skill.name)) {
      throw new Error(`invalid ${label} skill name`);
    }
    const logicalName = portableSkillCasefold(skill.name);
    const existing = byName.get(logicalName);
    if (existing) {
      const descriptions = [existing, skill]
        .map((candidate) => `${candidate.name}:${candidate.path ?? "<no-path>"}`)
        .sort(compareCodePoints);
      throw new Error(`conflicting ${label} logical skill name: ${logicalName} (${descriptions.join(", ")})`);
    }
    byName.set(logicalName, skill);
  }
  return byName;
}

function uniqueStrings(values) {
  return [...new Set(values.map(String).filter(Boolean))];
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
