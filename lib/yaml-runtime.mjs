import { parseDocument, stringify } from "yaml";

const ARCHITECTURE_PHASES = new Set([
  "design",
  "ddd-design",
  "architecture-review",
  "slice-plan",
  "plan-review",
]);
const PROFILE_PROMPT_EXCLUDED_FIELDS = new Set([
  "skills",
  "android_skills",
  "android_ecosystem_skills",
  "chrisbanes_skills",
]);

export function parseWorkflowYaml(text, name, source = `${name}.yaml`) {
  const raw = parseYamlMapping(text, `workflow ${source}`);
  const workflowId = raw.id ?? name;
  if (typeof workflowId !== "string" || !workflowId) {
    throw new Error(`workflow ${source}: id must be a non-empty string`);
  }
  if (!Array.isArray(raw.phases) || raw.phases.length === 0) {
    throw new Error(`workflow ${source}: missing or empty phases`);
  }
  const seenIds = new Set();
  const phases = raw.phases.map((item, index) => {
    if (!isMapping(item) || !Object.hasOwn(item, "id")) {
      throw new Error(`workflow ${source}: phase ${index} missing id`);
    }
    const id = requiredString(item.id, `workflow ${source}: phase ${index} id`);
    if (seenIds.has(id)) {
      throw new Error(`workflow ${source}: duplicate phase id ${JSON.stringify(id)} at index ${index}`);
    }
    seenIds.add(id);
    return {
      id,
      description: optionalString(item.description, "", `workflow ${source}: phase ${id} description`),
      prompt: optionalNullableString(item.prompt, `workflow ${source}: phase ${id} prompt`),
      pause_after: optionalBoolean(item.pause_after, false, `workflow ${source}: phase ${id} pause_after`),
      optional: optionalBoolean(item.optional, false, `workflow ${source}: phase ${id} optional`),
      multi_review: optionalBoolean(item.multi_review, false, `workflow ${source}: phase ${id} multi_review`),
      cite_lore: optionalBoolean(item.cite_lore, false, `workflow ${source}: phase ${id} cite_lore`),
      routes: optionalRoutes(item.routes, `workflow ${source}: phase ${id} routes`),
      required_markers: normalizeRequiredMarkers(item.required_markers),
      artifact: optionalString(item.artifact, defaultArtifact(workflowId, id), `workflow ${source}: phase ${id} artifact`),
    };
  });
  const phaseIds = new Set(phases.map((phase) => phase.id));
  for (const phase of phases) {
    if (phase.id === "multi-review" && phase.multi_review !== true) {
      throw new Error(`workflow ${source}: phase multi-review must set multi_review: true`);
    }
    if (
      (phase.multi_review || phase.id === "multi-review" || phase.id === "gates")
      && (!phase.routes || Object.keys(phase.routes).length === 0)
    ) {
      const kind = phase.multi_review ? "multi_review" : "gates";
      throw new Error(`workflow ${source}: phase ${phase.id} (${kind}) requires non-empty routes`);
    }
    for (const [key, target] of Object.entries(phase.routes ?? {})) {
      if (target !== "block" && !phaseIds.has(target)) {
        throw new Error(`workflow ${source}: phase ${phase.id} route ${JSON.stringify(key)} targets unknown phase ${target}`);
      }
    }
  }
  return { id: workflowId, source, phases };
}

export function parseInstalledProfileYaml(text, profileId, source = `${profileId}.yaml`) {
  const payload = parseYamlMapping(text, `installed profile ${source}`);
  if (payload.id !== profileId) {
    throw new Error(`installed profile id mismatch: ${profileId}`);
  }
  validateInstalledProfileShape(payload, profileId);
  return payload;
}

export function mergeProfilePayloads(profiles, primaryProfile = null) {
  const deduped = [];
  const seen = new Set();
  for (const [profileId, payload] of profiles) {
    validateSafeName(profileId, "profile");
    if (seen.has(profileId)) continue;
    if (!isMapping(payload) || payload.id !== profileId) {
      throw new Error(`profile id mismatch: ${profileId}`);
    }
    seen.add(profileId);
    deduped.push([profileId, payload]);
  }
  if (deduped.length === 0) throw new Error("at least one installed profile is required");
  if (deduped.length === 1) return deduped[0];

  const activeProfiles = deduped.map(([profileId]) => profileId);
  const primary = validateSafeName(primaryProfile ?? activeProfiles[0], "primary_profile");
  if (!activeProfiles.includes(primary)) {
    throw new Error("primary_profile must be contained in profiles");
  }
  return [activeProfiles.join(","), {
    id: "multi-profile",
    primary_profile: primary,
    active_profiles: activeProfiles,
    profiles: deduped.map(([, payload]) => payload),
    review_angles: mergeProfileListField(deduped, "review_angles"),
    gates: mergeProfileListField(deduped, "gates"),
    skills: Object.fromEntries(
      deduped.filter(([, payload]) => isMapping(payload.skills)).map(([id, payload]) => [id, payload.skills]),
    ),
    architecture: Object.fromEntries(
      deduped.filter(([, payload]) => isMapping(payload.architecture)).map(([id, payload]) => [id, payload.architecture]),
    ),
  }];
}

export function renderProfilePromptBlock(profileId, profile, phaseId) {
  const snapshot = profilePromptProjection(profile, phaseId);
  if (Object.keys(snapshot).length === 0) return "";
  const yaml = stringify(snapshot, {
    indent: 2,
    indentSeq: false,
    lineWidth: 0,
    schema: "yaml-1.1",
  }).trimEnd();
  return [
    `\n## Active profile: \`${profileId}\``,
    "",
    "This is the parsed profile YAML — use these values directly, don't ask the user to look them up:",
    "",
    "```yaml",
    yaml,
    "```",
    "",
  ].join("\n");
}

export function parseYamlMapping(text, label) {
  const document = parseDocument(text, {
    maxAliasCount: 0,
    prettyErrors: false,
    schema: "yaml-1.1",
    uniqueKeys: true,
  });
  if (document.errors.length > 0) {
    throw new Error(`${label}: invalid YAML: ${document.errors[0].message}`);
  }
  const value = document.toJS({ maxAliasCount: 0, mapAsMap: false });
  if (!isMapping(value)) throw new Error(`${label}: top-level must be a mapping`);
  return value;
}

function validateInstalledProfileShape(payload, profileId) {
  if (Object.hasOwn(payload, "branching")) {
    const branching = payload.branching;
    if (!isMapping(branching)) {
      throw new Error(`installed profile branching must be a mapping: ${profileId}`);
    }
    if (Object.hasOwn(branching, "worktree") && !["required", "optional", "disabled"].includes(branching.worktree)) {
      throw new Error(`installed profile branching.worktree must be required, optional, or disabled: ${profileId}`);
    }
    if (Object.hasOwn(branching, "base")) validateProfileBaseRef(branching.base, profileId);
    if (Object.hasOwn(branching, "naming")) {
      const naming = branching.naming;
      if (!isMapping(naming)) throw new Error(`installed profile branching.naming must be a mapping: ${profileId}`);
      if (Object.hasOwn(naming, "prefix") && naming.prefix !== "feat/") {
        throw new Error(`installed profile branching.naming.prefix must be feat/: ${profileId}`);
      }
      if (
        Object.hasOwn(naming, "max_slug_length")
        && (!Number.isInteger(naming.max_slug_length) || naming.max_slug_length < 12 || naming.max_slug_length > 100)
      ) {
        throw new Error(`installed profile branching.naming.max_slug_length must be an integer from 12 to 100: ${profileId}`);
      }
    }
  }
  for (const [field, kind] of [
    ["gates", "array"],
    ["review_angles", "array"],
    ["skills", "mapping"],
    ["architecture", "mapping"],
    ["artifacts", "mapping"],
    ["vocabulary", "mapping"],
    ["commit_convention", "mapping"],
    ["pr", "mapping"],
  ]) {
    if (!Object.hasOwn(payload, field)) continue;
    const valid = kind === "array" ? Array.isArray(payload[field]) : isMapping(payload[field]);
    const expected = kind === "array" ? "list" : "mapping";
    if (!valid) throw new Error(`installed profile ${field} must be a ${expected}: ${profileId}`);
  }
  for (const angle of payload.review_angles ?? []) {
    if (!isMapping(angle)) throw new Error(`installed profile review angle must be a mapping: ${profileId}`);
    if (typeof angle.id !== "string") throw new Error(`installed profile review angle id must be a string: ${profileId}`);
    validateSafeName(angle.id, "review angle");
  }
}

function validateProfileBaseRef(value, profileId) {
  const invalid = typeof value !== "string"
    || !value
    || value.startsWith("-")
    || value.startsWith("/")
    || value.endsWith("/")
    || value.endsWith(".")
    || value.endsWith(".lock")
    || value.includes("..")
    || value.includes("@{")
    || value.includes("//")
    || [...value].some((character) => /\s/u.test(character) || character.codePointAt(0) < 32)
    || /[~^:?*[\\]/u.test(value);
  if (invalid) throw new Error(`invalid profile base ref; installed profile branching.base is unsafe: ${profileId}`);
}

function profilePromptProjection(profile, phaseId) {
  if (!isMapping(profile)) return {};
  const projection = Object.fromEntries(
    Object.entries(profile).filter(([key]) => !PROFILE_PROMPT_EXCLUDED_FIELDS.has(key)),
  );
  if (Array.isArray(projection.profiles)) {
    projection.profiles = projection.profiles.map((value) => (
      isMapping(value) ? profilePromptProjection(value, phaseId) : value
    ));
  }
  if (isMapping(projection.architecture) && !ARCHITECTURE_PHASES.has(phaseId)) {
    projection.architecture = trimPromptArchitecture(projection.architecture);
  }
  return projection;
}

function trimPromptArchitecture(value) {
  return Object.fromEntries(
    Object.entries(value)
      .filter(([key]) => !["roles", "forbidden_dependencies"].includes(key))
      .map(([key, child]) => [key, isMapping(child) ? trimPromptArchitecture(child) : child]),
  );
}

function mergeProfileListField(profiles, field) {
  const merged = [];
  const seen = new Set();
  for (const [, profile] of profiles) {
    if (!Array.isArray(profile[field])) continue;
    for (const value of profile[field]) {
      const key = canonicalJson(value);
      if (seen.has(key)) continue;
      seen.add(key);
      merged.push(value);
    }
  }
  return merged;
}

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (isMapping(value)) {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function normalizeRequiredMarkers(value) {
  if (value === undefined || value === null) return [];
  if (typeof value === "string") return [value];
  if (Array.isArray(value)) return value.map(String);
  return [];
}

function defaultArtifact(workflowId, phaseId) {
  if (workflowId !== "full-feature") return `${phaseId}.md`;
  if (phaseId === "red") return "artifacts/red.log";
  if (phaseId === "green") return "artifacts/green.log";
  if (phaseId === "gates") return "artifacts/gate-results.json";
  return `artifacts/${phaseId}.md`;
}

function optionalRoutes(value, label) {
  if (value === undefined || value === null) return null;
  if (!isMapping(value)) throw new Error(`${label} must be a mapping`);
  for (const [key, target] of Object.entries(value)) {
    if (typeof key !== "string" || typeof target !== "string") {
      throw new Error(`${label} must map strings to strings`);
    }
  }
  return value;
}

function requiredString(value, label) {
  if (typeof value !== "string" || !value) throw new Error(`${label} must be a non-empty string`);
  return value;
}

function optionalString(value, fallback, label) {
  if (value === undefined || value === null) return fallback;
  if (typeof value !== "string") throw new Error(`${label} must be a string`);
  return value;
}

function optionalNullableString(value, label) {
  if (value === undefined || value === null) return null;
  if (typeof value !== "string") throw new Error(`${label} must be a string`);
  return value;
}

function optionalBoolean(value, fallback, label) {
  if (value === undefined || value === null) return fallback;
  if (typeof value !== "boolean") throw new Error(`${label} must be boolean`);
  return value;
}

function validateSafeName(value, label) {
  if (typeof value !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_-]*$/u.test(value)) {
    throw new Error(`invalid ${label}: ${JSON.stringify(value)}`);
  }
  return value;
}

function isMapping(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
